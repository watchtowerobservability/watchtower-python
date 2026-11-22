import hashlib
import io
import json

from watchtower.sampling import RemoteSampler, ratio_for, trace_fraction


class Resp(io.BytesIO):
    def __init__(self, body, etag):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def kept(s, n=400):
    return sum(1 for i in range(n) if s.should_sample(None, i + (7 << 96), "op").decision.is_sampled())


def test_ratio_for_rules():
    cfg = {"strategyType": "RULES", "rules": [
        {"match": {"status": "error"}, "action": {"kind": "drop"}},
        {"match": {"service": "api", "span_name_glob": "GET /health*"}, "action": {"kind": "drop"}},
        {"match": {"service": "api"}, "action": {"kind": "ratio", "ratio": 0.1}},
    ]}
    assert ratio_for(cfg, "api", "", "GET /health/live", {}) == 0
    assert ratio_for(cfg, "api", "", "GET /x", {}) == 0.1
    assert ratio_for(cfg, "web", "", "GET /x", {}) == 1


def test_fraction_is_spread_over_the_unit_interval():
    # random-looking ids like real ones: FNV-1a's top bits barely move when only the last bytes change
    ids = (int.from_bytes(hashlib.sha256(str(i).encode()).digest()[:16], "big") for i in range(2000))
    n = sum(1 for t in ids if trace_fraction(t) < 0.25)
    assert 350 < n < 650


def test_poll_applies_new_config_and_failure_keeps_the_last():
    state = {"rate": 1, "down": False}

    def opener(req, timeout=0):
        assert "/v1/sampling?service=svc" in req.full_url
        if state["down"]:
            raise OSError("down")
        return Resp({"strategyType": "PROBABILISTIC", "probabilisticSampling": {"samplingRate": state["rate"]}}, f'"{state["rate"]}"')

    s = RemoteSampler("http://x", {}, "svc", interval=3600, opener=opener)
    s.poll()
    assert kept(s) == 400
    state["rate"] = 0
    s.poll()
    assert kept(s) == 0
    state.update(rate=1, down=True)
    s.poll()
    assert kept(s) == 0
    s.stop()
