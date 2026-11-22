import threading
import urllib.parse

import pytest

from watchtower import profiling


def test_sampler_folds_other_threads_root_first():
    ready, done = threading.Event(), threading.Event()

    def busy_leaf():
        ready.set()
        done.wait(5)

    t = threading.Thread(target=busy_leaf)
    t.start()
    ready.wait(5)
    counts = {}
    profiling.sample_once(counts, threading.get_ident())
    done.set()
    t.join()
    stacks = [k.split(";") for k in counts if "busy_leaf" in k]
    assert stacks, counts
    # root first: the thread bootstrap comes before busy_leaf, and wait() below it
    names = stacks[0]
    assert names[0].startswith("_bootstrap") and any(n.startswith("busy_leaf") for n in names[1:])
    body = profiling.folded_body({"a;b": 3}).decode()
    assert body == "a;b 3\n"


def test_ingest_url_names_service_type_and_labels():
    u = profiling.ingest_url("http://gw:4318/", "api", {"env": "prod", "version": None}, 10, 25, 100)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    assert u.startswith("http://gw:4318/ingest?")
    assert q["name"] == ["api.wall{env=prod}"]
    assert (q["from"], q["until"], q["format"]) == (["10"], ["25"], ["folded"])


def test_requires_a_key(monkeypatch):
    monkeypatch.delenv("WT_INGEST_KEY", raising=False)
    with pytest.raises(ValueError):
        profiling.Profiler()
    p = profiling.Profiler(api_key="k", endpoint="http://127.0.0.1:9", period=1)
    p.upload({"a": 1}, 0, 1)
    assert p.dropped == 1


def test_span_labels_this_threads_samples_only():
    counts = {}
    with profiling.span(span_id=0xab, trace_id=0xcd):
        profiling.sample_once(counts)
    assert any(k.startswith("span_id=00000000000000ab,trace_id=" + "0" * 30 + "cd\t") for k in counts)
    counts = {}
    profiling.sample_once(counts)
    assert not any(k.startswith("span_id=") for k in counts)


def test_span_follows_its_task_across_await():
    import asyncio

    seen = {}

    def sample(name):
        seen[name] = {}
        profiling.sample_once(seen[name])

    async def labeled(gate):
        with profiling.span(span_id=0xab, trace_id=0xcd):
            await gate.wait()
            sample("after_await")

    async def other(gate):
        await asyncio.sleep(0)
        sample("other_task")  # runs while the labeled task waits
        gate.set()

    async def main():
        gate = asyncio.Event()
        await asyncio.gather(labeled(gate), other(gate))

    asyncio.run(main())
    lab = "span_id=00000000000000ab,"
    assert any(k.startswith(lab) for k in seen["after_await"])
    assert not any(k.startswith(lab) for k in seen["other_task"])
    assert not profiling._spans
