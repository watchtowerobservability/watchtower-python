import asyncio
import functools
import json
import pathlib
import sqlite3

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from watchtower import sqlnorm
from watchtower.integrations import datastores as ds
from watchtower.integrations import kvstores as kv

_VECTORS = pathlib.Path(__file__).resolve().parents[3] / "testdata" / "sql_normalize_vectors.json"


def _vectors():
    if not _VECTORS.exists():  # the published SDK mirror does not carry the shared contract
        return []
    return json.loads(_VECTORS.read_text())["vectors"]


@pytest.mark.skipif(not _VECTORS.exists(), reason="shared vectors not in this checkout")
@pytest.mark.parametrize("v", _vectors(), ids=lambda v: v["name"])
def test_shared_vectors(v):
    assert sqlnorm.normalize(v["input"], v["pg"]) == v["normalized"]
    assert sqlnorm.query_hash(v["normalized"]) == v["hash"]


@pytest.mark.parametrize("text,op,table", [
    ("SELECT * FROM users WHERE id = ?", "SELECT", "users"),
    ('update "public"."jobs" set a = ?', "UPDATE", "public.jobs"),
    ("WITH x AS (SELECT ?) DELETE FROM t", "DELETE", "t"),
    ("PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA", ""),
])
def test_summary(text, op, table):
    assert sqlnorm.summary(text) == (op, table)


def _values(reader):
    out = {}
    data = reader.get_metrics_data()  # None once nothing is observed (newer SDKs)
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    out[(m.name, p.attributes.get("db.namespace"))] = p.value
    return out


@pytest.fixture
def mp():
    reader = InMemoryMetricReader()
    return MeterProvider(metric_readers=[reader]), reader


def test_sqlite3_spans_hash_not_text(cap, tmp_path):
    path = str(tmp_path / "app.db")
    conn = ds.instrument_sqlite3(sqlite3.connect(path))
    conn.execute("create table users (id integer, email text)")
    conn.execute("insert into users values (1, 'alice@example.com')")
    cur = conn.cursor()
    cur.execute("select * from users where email = ?", ("alice@example.com",))
    assert cur.fetchall() == [(1, "alice@example.com")]
    conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("select * from missing")
    names = [s.name for s in cap.spans]
    assert names[:3] == ["CREATE users", "INSERT users", "SELECT users"]
    assert "COMMIT" in names
    a = cap.spans[1].attributes
    assert a["db.system.name"] == "sqlite" and a["db.namespace"].endswith("app.db") and a["db.query.hash"].isdigit()
    assert "db.query.text" not in a and "alice" not in str([dict(s.attributes) for s in cap.spans])
    assert cap.spans[-1].status.status_code.name == "ERROR"


def test_sqlite3_opt_in_text_is_sanitized(cap):
    conn = ds.instrument_sqlite3(sqlite3.connect(":memory:"), with_text=True)
    conn.execute("select 'secret' as x")
    assert cap.spans[0].attributes["db.query.text"] == "select ? as x"


def test_aiosqlite_style_async_execute(cap):
    class Cursor:
        async def close(self):
            self.closed = True

    class Conn:
        def execute(self, sql, *a):
            async def run():
                return Cursor()
            return run()

    conn = ds.instrument_aiosqlite(Conn(), path="a.db")

    async def main():
        await conn.execute("select 1")
        async with conn.execute("delete from jobs") as cur:
            return cur

    cur = asyncio.run(main())
    assert cur.closed and [s.name for s in cap.spans] == ["SELECT", "DELETE jobs"]


def test_observe_files_and_unregister(mp, tmp_path):
    provider, reader = mp
    p = tmp_path / "x.db"
    p.write_bytes(b"\0" * 2048)
    (tmp_path / "x.db-wal").write_bytes(b"\0" * 10)
    h = ds.observe_sqlite(str(p), meter_provider=provider)
    ds.observe("custom", "ns", lambda: {"not.ours": 1, "wt.embedded.keys": -1}, meter_provider=provider)
    v = _values(reader)
    assert v[("wt.embedded.file.size", str(p))] == 2048 and v[("wt.embedded.wal.size", str(p))] == 10
    assert ("wt.embedded.keys", "ns") not in v  # negative = unknown
    h.unregister()
    assert ("wt.embedded.file.size", str(p)) not in _values(reader)


def test_lmdb_transaction_span(cap, mp):
    class Txn:
        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def put(self, k, v):
            return True

    class Env:
        def path(self):
            return "/data/lmdb"

        def begin(self, db=None, parent=None, write=False, buffers=False):
            return Txn()

        def info(self):
            return {"map_size": 1 << 20, "last_pgno": 9, "num_readers": 2}

        def stat(self):
            return {"psize": 4096, "entries": 5}

    env = kv.instrument_lmdb(Env())
    with env.begin(write=True) as txn:
        txn.put(b"k", b"v")
    env.begin()  # a read txn not yet ended has no finished span
    assert [s.name for s in cap.spans] == ["write"]
    provider, reader = mp
    kv.observe_lmdb(env, meter_provider=provider)
    v = _values(reader)
    assert v[("wt.embedded.map.used", "/data/lmdb")] == 40960 and v[("wt.embedded.readers", "/data/lmdb")] == 2


def test_rocksdict_miss_is_not_error(cap):
    db = kv.instrument_rocksdict({}, path="/r")
    db["a"] = b"1"
    assert db["a"] == b"1"
    with pytest.raises(KeyError):
        db["missing"]
    assert [s.name for s in cap.spans] == ["put", "get", "get"]
    assert cap.spans[2].status.status_code.name != "ERROR"


def test_cachetools_counts_hits_misses_evictions(mp):
    cachetools = pytest.importorskip("cachetools")
    provider, reader = mp
    c = cachetools.LRUCache(maxsize=1)
    kv.instrument_cachetools(c, "sessions", meter_provider=provider)

    @cachetools.cached(cache=c)
    def f(x):
        return x * 2

    f(1), f(1), f(2)  # miss, hit, miss + eviction
    v = _values(reader)
    assert v[("wt.embedded.cache.hits", "sessions")] == 1 and v[("wt.embedded.cache.misses", "sessions")] == 2
    assert v[("wt.embedded.cache.evictions", "sessions")] == 1 and isinstance(c, cachetools.LRUCache)


def test_lru_cache(mp):
    provider, reader = mp

    @functools.lru_cache(maxsize=8)
    def g(x):
        return x

    g(1), g(1)
    kv.observe_lru_cache(g, "g", meter_provider=provider)
    v = _values(reader)
    assert v[("wt.embedded.cache.hits", "g")] == 1 and v[("wt.embedded.keys", "g")] == 1
