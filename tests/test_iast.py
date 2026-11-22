import json

from watchtower import iast


def setup_function():
    iast.stop_iast()


def start(sent):
    iast.start_iast("https://api.example/", "wtk_x", "prj_1", "checkout", flush_s=60,
                    post=lambda url, headers, body: sent.append((url, headers, json.loads(body))))


def test_sql_built_from_request_input_is_reported_once():
    sent = []
    start(sent)

    def execute(sql, params=()):
        return sql

    run = iast.guard_sink(execute, "sql_injection")

    def app(environ, start_response):
        q = environ["QUERY_STRING"].split("=", 1)[1]
        run("SELECT * FROM users WHERE name = '" + q + "'")  # tainted
        run("SELECT * FROM users WHERE name = %s", (q,))  # parameterised: not tainted
        run("SELECT 1")
        return []

    mw = iast.IastMiddleware(app)
    mw({"QUERY_STRING": "name=bob%27+OR+1%3D1", "PATH_INFO": "/u"}, None)
    mw({"QUERY_STRING": "name=bob%27+OR+1%3D1", "PATH_INFO": "/u"}, None)  # same site: deduplicated
    iast.flush_iast()
    assert len(sent) == 1
    url, headers, body = sent[0]
    assert url == "https://api.example/v1/projects/prj_1/code-security/runtime"
    assert headers["Authorization"] == "Bearer wtk_x"
    assert body["service"] == "checkout"
    assert [f["vuln_type"] for f in body["findings"]] == ["sql_injection"]
    assert body["findings"][0]["code_location"].startswith(__file__)


def test_off_by_default_and_outside_a_request():
    run = iast.guard_sink(lambda s: s, "command_injection")
    assert run("rm -rf /tmp/xxxx") == "rm -rf /tmp/xxxx"
    sent = []
    start(sent)
    assert iast.check_sink("command_injection", "rm -rf /tmp/xxxx") is False  # no request context
    iast.flush_iast()
    assert sent == []
