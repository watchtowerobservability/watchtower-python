"""Embedded and in-memory datastores: client spans (OTel db semconv) and `wt.embedded.*` health metrics.

SQL engines (sqlite3, aiosqlite, apsw, duckdb) put `db.query.hash` and `db.query.summary` on spans; the
raw statement is never recorded, `with_text=True` adds it sanitized. Key-value stores and caches live in
`kvstores`. Every `instrument_*` returns a proxy that forwards everything else to the wrapped object;
`observe_*` returns a handle whose `unregister()` stops the metrics. No store is imported here: pass
the object you already have, so no extra is needed."""
import os
import threading
import time
import weakref

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .. import client as _c
from .. import sqlnorm

GAUGES = {
    "wt.embedded.file.size": "By", "wt.embedded.wal.size": "By", "wt.embedded.map.size": "By",
    "wt.embedded.map.used": "By", "wt.embedded.memory.used": "By", "wt.embedded.cache.size": "By",
    "wt.embedded.keys": "{key}", "wt.embedded.compaction.pending": "By",
    "wt.embedded.transactions.open": "{transaction}", "wt.embedded.free_pages": "{page}",
    "wt.embedded.levels.files": "{file}", "wt.embedded.snapshots.open": "{snapshot}",
    "wt.embedded.iterators.open": "{iterator}", "wt.embedded.readers": "{reader}",
}
COUNTERS = {
    "wt.embedded.cache.hits": "{hit}", "wt.embedded.cache.misses": "{miss}",
    "wt.embedded.cache.evictions": "{key}", "wt.embedded.cache.rejections": "{key}",
    "wt.embedded.lock.waits": "{wait}", "wt.embedded.lock.wait_time": "ms",
    "wt.embedded.compactions": "{compaction}", "wt.embedded.compaction.duration": "ms",
    "wt.embedded.flushes": "{flush}", "wt.embedded.transactions": "{transaction}",
    "wt.embedded.bytes.written": "By", "wt.embedded.bytes.read": "By",
}


def _tracer():
    return _c._client.tracer if _c._client else trace.get_tracer("watchtower.datastores")


def _fail(span, e):
    span.record_exception(e)
    span.set_status(Status(StatusCode.ERROR, str(e)))
    span.set_attribute("error.type", type(e).__qualname__)


def start_span(system, op, namespace=None, collection=None, attrs=None):
    """A client span for one store operation; the caller ends it."""
    a = {"db.system.name": system, "db.operation.name": op}
    if namespace:
        a["db.namespace"] = str(namespace)
    if collection:
        a["db.collection.name"] = str(collection)
    a.update(attrs or {})
    name = a.get("db.query.summary") or (f"{op} {collection}" if collection else op)
    return _tracer().start_span(name, kind=SpanKind.CLIENT, attributes=a)


class _Awaiting:
    """Ends the span when the awaitable finishes; also usable as `async with` (aiosqlite's execute)."""

    def __init__(self, aw, span):
        self._aw, self._span, self._res = aw, span, None

    def __await__(self):
        return self._run().__await__()

    async def _run(self):
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                return await self._aw
        except Exception as e:
            _fail(self._span, e)
            raise
        finally:
            self._span.end()

    async def __aenter__(self):
        self._res = await self
        return self._res

    async def __aexit__(self, *exc):
        close = getattr(self._res, "close", None)
        if close is not None:
            await close()


def traced(fn, system, op, namespace=None, collection=None, sql=False, with_text=False, ok=()):
    """Wrap `fn` in a client span. `sql=True` reads the statement from the first argument (hash and
    summary only). Exceptions in `ok` (e.g. KeyError on a miss) do not fail the span."""

    def inner(*a, **kw):
        attrs = sqlnorm.attributes(a[0], with_text, system) if sql and a and isinstance(a[0], str) else {}
        span = start_span(system, attrs.get("db.operation.name", op), namespace, collection or attrs.get("db.collection.name"), attrs)
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                res = fn(*a, **kw)
        except ok:
            span.end()
            raise
        except Exception as e:
            _fail(span, e)
            span.end()
            raise
        if hasattr(res, "__await__"):
            return _Awaiting(res, span)
        span.end()
        return res

    inner.__wrapped__ = fn
    return inner


class Proxy:
    """Forwards to `obj`; attributes named in `wraps` go through `wraps[name](attr)` first."""

    def __init__(self, obj, wraps):
        object.__setattr__(self, "_wt_obj", obj)
        object.__setattr__(self, "_wt_wraps", wraps)

    def __getattr__(self, name):
        v = getattr(self._wt_obj, name)
        w = self._wt_wraps.get(name)
        return w(v) if w is not None and callable(v) else v

    def __setattr__(self, name, value):
        setattr(self._wt_obj, name, value)

    def _wt_dunder(self, name, *a):
        fn = getattr(self._wt_obj, name)
        w = self._wt_wraps.get(name)
        return (w(fn) if w is not None else fn)(*a)

    def __enter__(self):
        self._wt_obj.__enter__()
        return self

    def __exit__(self, *exc):
        return self._wt_dunder("__exit__", *exc)

    def __getitem__(self, k):
        return self._wt_dunder("__getitem__", k)

    def __setitem__(self, k, v):
        self._wt_dunder("__setitem__", k, v)

    def __delitem__(self, k):
        self._wt_dunder("__delitem__", k)

    def __contains__(self, k):
        return k in self._wt_obj

    def __iter__(self):
        return iter(self._wt_obj)

    def __len__(self):
        return len(self._wt_obj)

    def __repr__(self):
        return f"<watchtower proxy of {self._wt_obj!r}>"


# --- metrics ---------------------------------------------------------------------------------

class Handle:
    def __init__(self, reg, src):
        self._reg, self._src = reg, src

    def unregister(self):
        with self._reg.lock:
            if self._src in self._reg.sources:
                self._reg.sources.remove(self._src)


class _Registry:
    """One set of instruments per meter provider; each reads every registered source (cached for
    one second so a collection calls each source once)."""

    def __init__(self, meter):
        self.sources, self.lock = [], threading.Lock()
        for name, unit in GAUGES.items():
            meter.create_observable_gauge(name, [self._cb(name)], unit=unit)
        for name, unit in COUNTERS.items():
            meter.create_observable_counter(name, [self._cb(name)], unit=unit)

    def _cb(self, name):
        def cb(_options):
            with self.lock:
                sources = list(self.sources)
            out = []
            for src in sources:
                v = _read(src).get(name)
                if v is not None and v >= 0:
                    out.append(metrics.Observation(int(v), src["attrs"]))
            return out

        return cb


def _read(src):
    now = time.monotonic()
    if now - src["at"] > 1.0:
        try:
            src["last"] = src["fn"]() or {}
        except Exception:  # a closed store must not break the whole collection
            src["last"] = {}
        src["at"] = now
    return src["last"]


_registries = weakref.WeakKeyDictionary()  # a provider id can be reused after it is collected
_reg_lock = threading.Lock()


def observe(system, namespace, fn, meter_provider=None):
    """Read `fn() -> {metric name: value}` at each collection, tagged {db.system.name, db.namespace}.
    Names outside GAUGES/COUNTERS and negative values are ignored; counters take cumulative totals."""
    mp = meter_provider or metrics.get_meter_provider()
    with _reg_lock:
        reg = _registries.get(mp)
        if reg is None:
            reg = _registries[mp] = _Registry(mp.get_meter("watchtower.datastores"))
    src = {"fn": fn, "at": -1e9, "last": {},
           "attrs": {"db.system.name": system, "db.namespace": str(namespace)}}
    with reg.lock:
        reg.sources.append(src)
    return Handle(reg, src)


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def dir_size(path):
    """Total bytes of the files under `path` (directory-backed stores: LevelDB, RocksDB, LMDB dir)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            total += file_size(os.path.join(root, f)) or 0
    return total


def observe_files(system, path, wal_path=None, meter_provider=None):
    """wt.embedded.file.size and wal.size for a file-backed store (SQLite: path + "-wal"; DuckDB: path + ".wal")."""
    return observe(system, path, lambda: {"wt.embedded.file.size": file_size(path),
                                          "wt.embedded.wal.size": file_size(wal_path) if wal_path else None},
                   meter_provider)


# --- SQL engines -----------------------------------------------------------------------------

def _sql_wraps(system, ns, with_text, methods, async_=False):
    w = {m: (lambda f, m=m: traced(f, system, m, ns, sql=True, with_text=with_text)) for m in methods}
    for m in ("commit", "rollback"):
        w[m] = lambda f, m=m: traced(f, system, m.upper(), ns)
    return w


def _sqlite_path(conn):
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or ":memory:"
    except Exception:
        pass
    return None


def instrument_sqlite3(conn, path=None, with_text=False):
    """Trace a `sqlite3.Connection` (stdlib; also pysqlite3 and sqlcipher3): execute, executemany,
    executescript, cursors, commit, rollback."""
    ns = path or _sqlite_path(conn)
    stmts = ("execute", "executemany", "executescript")
    w = _sql_wraps("sqlite", ns, with_text, stmts)
    w["cursor"] = lambda f: lambda *a, **kw: Proxy(f(*a, **kw), _sql_wraps("sqlite", ns, with_text, stmts))
    return Proxy(conn, w)


def observe_sqlite(path, meter_provider=None):
    """File and WAL size of a SQLite database (any driver)."""
    return observe_files("sqlite", path, path + "-wal", meter_provider)


def instrument_aiosqlite(conn, path=None, with_text=False):
    """Trace an `aiosqlite.Connection`: execute (also `async with conn.execute(...)`), executemany,
    executescript, execute_fetchall, execute_insert, commit, rollback."""
    stmts = ("execute", "executemany", "executescript", "execute_fetchall", "execute_insert")
    return Proxy(conn, _sql_wraps("sqlite", path, with_text, stmts))


def instrument_apsw(conn, with_text=False):
    """Trace an `apsw.Connection`: execute, executemany and cursor().execute."""
    ns = getattr(conn, "filename", None)
    stmts = ("execute", "executemany")
    w = _sql_wraps("sqlite", ns, with_text, stmts)
    w["cursor"] = lambda f: lambda *a, **kw: Proxy(f(*a, **kw), _sql_wraps("sqlite", ns, with_text, stmts))
    return Proxy(conn, w)


def observe_apsw(conn, apsw, meter_provider=None):
    """Page-cache hits/misses/bytes from sqlite3_db_status, process SQLite memory and file/WAL size.
    Pass the `apsw` module (not imported here)."""
    path = getattr(conn, "filename", "") or ":memory:"

    def read():
        st = lambda op: conn.status(op, False)[0]  # noqa: E731
        return {"wt.embedded.cache.hits": st(apsw.SQLITE_DBSTATUS_CACHE_HIT),
                "wt.embedded.cache.misses": st(apsw.SQLITE_DBSTATUS_CACHE_MISS),
                "wt.embedded.cache.size": st(apsw.SQLITE_DBSTATUS_CACHE_USED),
                "wt.embedded.memory.used": apsw.status(apsw.SQLITE_STATUS_MEMORY_USED)[0],
                "wt.embedded.file.size": file_size(path), "wt.embedded.wal.size": file_size(path + "-wal")}

    return observe("sqlite", path, read, meter_provider)


def instrument_duckdb(conn, path=":memory:", with_text=False):
    """Trace a `duckdb.DuckDBPyConnection`: execute, executemany, sql, query, commit, rollback."""
    return Proxy(conn, _sql_wraps("duckdb", path, with_text, ("execute", "executemany", "sql", "query")))


def observe_duckdb(conn, path=":memory:", meter_provider=None):
    """Block usage, WAL and buffer-manager memory, read on a cursor (its own connection, so it is
    safe from the metrics thread)."""
    cur = conn.cursor()

    def read():
        bs, total, used = cur.execute("SELECT block_size, total_blocks, used_blocks FROM pragma_database_size()").fetchone()
        mem = cur.execute("SELECT sum(memory_usage_bytes) FROM duckdb_memory()").fetchone()[0]
        return {"wt.embedded.map.size": bs * total, "wt.embedded.map.used": bs * used, "wt.embedded.memory.used": mem,
                "wt.embedded.file.size": file_size(path), "wt.embedded.wal.size": file_size(path + ".wal")}

    return observe("duckdb", path, read, meter_provider)
