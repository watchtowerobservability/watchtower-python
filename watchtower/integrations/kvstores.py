"""Embedded key-value stores and in-process caches: LMDB, LevelDB (plyvel), RocksDB (rocksdict), TinyDB,
diskcache, cachetools and functools.lru_cache. Spans never carry keys or values. Shared model and
proxy in `datastores`."""
from .datastores import Proxy, _fail, dir_size, file_size, observe, start_span, traced

_OP = {"__setitem__": "put", "__delitem__": "delete"}


def _ops(system, ns, names):
    return {n: (lambda f, n=n: traced(f, system, _OP.get(n, n), ns)) for n in names}


# --- LMDB ------------------------------------------------------------------------------------

class _LmdbTxn(Proxy):
    """A span covering one LMDB transaction, from begin() to commit/abort/context exit."""

    def __init__(self, txn, span):
        super().__init__(txn, {})
        object.__setattr__(self, "_wt_span", span)

    def _end(self, err=None):
        if err is not None:
            _fail(self._wt_span, err)
        self._wt_span.end()

    def commit(self):
        try:
            return self._wt_obj.commit()
        finally:
            self._end()

    def abort(self):
        try:
            return self._wt_obj.abort()
        finally:
            self._end()

    def __exit__(self, et, ev, tb):
        try:
            return self._wt_obj.__exit__(et, ev, tb)
        finally:
            self._end(ev)


def instrument_lmdb(env):
    """Trace an `lmdb.Environment`: one span per transaction (begin -> commit/abort), named "write" or "read"."""
    ns = env.path()

    def begin(f):
        def inner(*a, **kw):
            write = kw.get("write", a[2] if len(a) > 2 else False)  # begin(db, parent, write, buffers)
            span = start_span("lmdb", "write" if write else "read", ns)
            try:
                return _LmdbTxn(f(*a, **kw), span)
            except Exception as e:
                _fail(span, e)
                span.end()
                raise
        return inner

    return Proxy(env, {"begin": begin, "copy": lambda f: traced(f, "lmdb", "copy", ns)})


def observe_lmdb(env, meter_provider=None):
    """Map size and use, entries, readers and file size from env.info() and env.stat()."""
    def read():
        i, s = env.info(), env.stat()
        return {"wt.embedded.map.size": i["map_size"], "wt.embedded.map.used": (i["last_pgno"] + 1) * s["psize"],
                "wt.embedded.keys": s["entries"], "wt.embedded.readers": i["num_readers"],
                "wt.embedded.file.size": dir_size(env.path())}

    return observe("lmdb", env.path(), read, meter_provider)


# --- LevelDB (plyvel) and RocksDB (rocksdict) ------------------------------------------------

def instrument_plyvel(db, path=None):
    """Trace a `plyvel.DB`: get, put, delete and write_batch (the batch's commit, as a context manager or write())."""
    ns = path or getattr(db, "name", None)
    w = _ops("leveldb", ns, ("get", "put", "delete"))
    w["write_batch"] = lambda f: lambda *a, **kw: Proxy(f(*a, **kw), {"write": lambda g: traced(g, "leveldb", "write", ns),
                                                                      "__exit__": lambda g: traced(g, "leveldb", "write", ns)})
    return Proxy(db, w)


def observe_plyvel(db, path, meter_provider=None):
    """Directory size and approximate memory from LevelDB properties."""
    def read():
        mem = db.get_property(b"leveldb.approximate-memory-usage")
        return {"wt.embedded.file.size": dir_size(path), "wt.embedded.memory.used": int(mem) if mem else None}

    return observe("leveldb", path, read, meter_provider)


def instrument_rocksdict(db, path=None):
    """Trace a `rocksdict.Rdict`: get/put/delete/write, `db[k]` (a KeyError miss is not an error) and `db[k] = v`."""
    w = _ops("rocksdb", path, ("get", "put", "delete", "write", "__setitem__", "__delitem__"))
    w["__getitem__"] = lambda f: traced(f, "rocksdb", "get", path, ok=(KeyError,))
    return Proxy(db, w)


_ROCKS = {"wt.embedded.keys": "rocksdb.estimate-num-keys", "wt.embedded.cache.size": "rocksdb.block-cache-usage",
          "wt.embedded.compaction.pending": "rocksdb.estimate-pending-compaction-bytes",
          "wt.embedded.memory.used": "rocksdb.cur-size-all-mem-tables",
          "wt.embedded.file.size": "rocksdb.total-sst-files-size",
          "wt.embedded.snapshots.open": "rocksdb.num-snapshots"}


def observe_rocksdict(db, path, meter_provider=None):
    """Keys, block cache, memtables, SST size, pending compaction bytes and snapshots from RocksDB properties."""
    return observe("rocksdb", path, lambda: {k: db.property_int_value(p) for k, p in _ROCKS.items()}, meter_provider)


# --- TinyDB ----------------------------------------------------------------------------------

_TINY = ("insert", "insert_multiple", "search", "get", "update", "update_multiple", "upsert", "remove", "all", "truncate", "count")


def instrument_tinydb(db, path=None):
    """Trace a `tinydb.TinyDB` (default table) and the tables from db.table(name), with the table as collection."""
    def table(f):
        def inner(name, *a, **kw):
            return Proxy(f(name, *a, **kw), {m: (lambda g, m=m: traced(g, "tinydb", m, path, name)) for m in _TINY})
        return inner

    w = {m: (lambda g, m=m: traced(g, "tinydb", m, path, "_default")) for m in _TINY}
    w["table"] = table
    return Proxy(db, w)


def observe_tinydb(db, path, meter_provider=None):
    """Document count of the default table and the JSON file size."""
    return observe("tinydb", path, lambda: {"wt.embedded.keys": len(db), "wt.embedded.file.size": file_size(path)},
                   meter_provider)


# --- caches (metrics only: too hot to trace per call) ----------------------------------------

def observe_diskcache(cache, meter_provider=None):
    """Hits, misses, on-disk volume and key count of a `diskcache.Cache` (turns stats on)."""
    cache.stats(enable=True)

    def read():
        hits, misses = cache.stats()
        return {"wt.embedded.cache.hits": hits, "wt.embedded.cache.misses": misses,
                "wt.embedded.file.size": cache.volume(), "wt.embedded.keys": len(cache)}

    return observe("diskcache", cache.directory, read, meter_provider)


def instrument_cachetools(cache, name, meter_provider=None):
    """Count hits, misses and evictions on a cachetools cache (Cache, LRUCache, TTLCache, LFUCache, ...)
    in place, and report them with size and key count. Returns the metrics handle."""
    stats = {"hits": 0, "misses": 0, "evictions": 0}
    base = type(cache)

    class Counted(base):
        def __getitem__(self, key):
            try:
                v = base.__getitem__(self, key)
            except KeyError:
                stats["misses"] += 1
                raise
            stats["hits"] += 1
            return v

        def popitem(self):
            stats["evictions"] += 1
            return base.popitem(self)

    Counted.__name__ = base.__name__
    cache.__class__ = Counted  # same layout, no new slots: only the methods above change
    return observe("cachetools", name, lambda: {
        "wt.embedded.cache.hits": stats["hits"], "wt.embedded.cache.misses": stats["misses"],
        "wt.embedded.cache.evictions": stats["evictions"], "wt.embedded.keys": len(cache),
        "wt.embedded.cache.size": cache.currsize}, meter_provider)


def observe_lru_cache(fn, name=None, meter_provider=None):
    """Hits, misses and size of a functools.lru_cache / functools.cache function."""
    def read():
        i = fn.cache_info()
        return {"wt.embedded.cache.hits": i.hits, "wt.embedded.cache.misses": i.misses, "wt.embedded.keys": i.currsize}

    return observe("functools.lru_cache", name or fn.__qualname__, read, meter_provider)


__all__ = ["instrument_lmdb", "observe_lmdb", "instrument_plyvel", "observe_plyvel", "instrument_rocksdict",
           "observe_rocksdict", "instrument_tinydb", "observe_tinydb", "observe_diskcache", "instrument_cachetools",
           "observe_lru_cache"]
