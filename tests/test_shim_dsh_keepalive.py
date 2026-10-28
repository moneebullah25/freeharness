"""Real dsh against the real shim HTTP handler: a reply that takes a while arrives intact when the shim sends its HTTP headers
early and SSE keepalive comments (a Node HTTP client drops a response with no headers after 300s; MEASURED live 2026-10-28).
No browser, no live provider; skipped without node/dsh."""
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

import pytest
from test_shim import Clock, FakeSession

from freeharness.backends import shim
from freeharness.client import rpc

try:
    rpc.find_node_dir()
    HAVE = rpc.DSH_BIN.exists()
except Exception:  # noqa: BLE001
    HAVE = False
pytestmark = pytest.mark.skipif(not HAVE, reason="node / dsh harness not installed")


class SlowSession(FakeSession):
    def ask(self, prompt, fresh=True, should_stop=None):
        time.sleep(3.0)
        yield from super().ask(prompt, fresh, should_stop)


def test_real_dsh_accepts_early_headers_and_keepalive_comments_and_gets_the_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(shim, "PING_SECONDS", 0.5)
    sess = SlowSession(reply="the keepalive answer")
    shim.Handler.router = shim.Router(["gemini"], lambda prov: sess, clock=Clock(), log=lambda m: None, sticky=["gemini"])
    shim.Handler.worker = shim.Worker()
    threading.Thread(target=shim.Handler.worker.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    patch = tmp_path / "patch.yml"
    patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    rt = rpc.Runtime(proj, home=home, patch=patch, overlay_files=[])
    try:
        rt.start()
        r = rt.run_turn(f"ka-{uuid.uuid4().hex[:8]}", "say something", timeout=120)
    finally:
        rt.close()
        srv.shutdown()
        shim.Handler.router = None
    assert r.ok, r.reason
    assert "the keepalive answer" in (r.text if hasattr(r, "text") else str(r))
    assert sess.prompts, "the request reached the shim"
