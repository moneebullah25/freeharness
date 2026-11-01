import json

from freeharness import oss_cmd
from freeharness.eval import oss_judge as oj
from freeharness.redact import redact


def test_github_tokens_and_api_keys():
    for tok in ("ghp_" + "a" * 36, "gho_" + "B1" * 18, "ghs_" + "c" * 30, "github_pat_" + "x" * 40, "sk-" + "q" * 30,
                "AKIA" + "A" * 16):
        out = redact(f"token is {tok} ok")
        assert tok not in out and "***" in out


def test_bearer_and_authorization_headers():
    assert "abc123def456" not in redact("curl -H 'Authorization: Bearer abc123def456' x")
    assert "hunter2xyz" not in redact("Authorization: Basic hunter2xyz")
    assert "Bearer ***" in redact("got Bearer abcdefghijkl1234")


def test_private_key_block_even_json_escaped():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef==\n-----END RSA PRIVATE KEY-----"
    assert "MIIEabc" not in redact("x " + pem + " y")
    line = redact(json.dumps({"text": pem}))
    assert "MIIEabc" not in line and json.loads(line)["text"] == "***"


def test_secret_assignments_keep_the_name():
    out = redact("export GITHUB_API_KEY=0123456789abcdef0123456789abcdef and MY_SECRET: \"Zm9vYmFyYmF6cXV4MTIzNDU2\"")
    assert "0123456789abcdef" not in out and "Zm9vYmFy" not in out
    assert "GITHUB_API_KEY=***" in out
    assert "hunter2hunter2" not in redact("DB_PASSWORD=hunter2hunter2")
    assert redact("KEY=short") == "KEY=short"              # not a long secret
    assert redact("api_token = None") == "api_token = None"


def test_emails_all_redacted():
    out = redact("mail moneebullah25@gmail.com and a.b+c@ex-ample.co.uk")
    assert "@" not in out and out.count("<email>") == 2


def test_plain_text_untouched_and_empty():
    assert redact("") == "" and redact("def f(x): return x @ y") == "def f(x): return x @ y"


def test_json_lines_stay_valid():
    rec = {"text": "ghp_" + "z" * 30 + " Authorization: Bearer abcdefgh1234 SECRET_KEY=abcdefghijklmnopqrst a@b.io", "n": 3}
    back = json.loads(redact(json.dumps(rec)))
    assert back["n"] == 3 and "ghp_" not in back["text"] and "a@b.io" not in back["text"]


def test_scrub_and_recorder_apply_it(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert "me@x.org" not in oss_cmd._scrub("hello me@x.org")
    rd = oj.RunDir(tmp_path)
    rec = oss_cmd.Recorder(rd)
    rec.note("text", "contact me@x.org with ghp_" + "k" * 30)
    rec.close()
    row = json.loads(rd.supervisor.read_text().splitlines()[0])
    assert row["kind"] == "text" and "me@x.org" not in row["text"] and "ghp_" not in row["text"]
