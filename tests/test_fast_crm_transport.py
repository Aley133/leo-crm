import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from tools import kaspi_fast_dumping_agent as agent


@pytest.fixture
def crm_server():
    agent._close_crm_client()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.server.connections += 1

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.received.append((self.path, self.headers.get("Authorization"), payload))
            code = int(self.path.rsplit("/", 1)[-1]) if self.path.startswith("/status/") else 200
            body = json.dumps({"ok": code == 200}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if code == 503:
                self.send_header("Retry-After", "7")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.connections = 0
    server.received = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        agent._close_crm_client()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_claim_completion_and_heartbeat_reuse_one_connection(crm_server):
    server, url = crm_server
    for path, token in [("claim", "first"), ("scan-complete", "second"), ("heartbeat", "second")]:
        assert agent._post_json(f"{url}/{path}", token, {"stage": path}) == {"ok": True}
    assert server.connections == 1
    assert [r[1] for r in server.received] == ["Bearer first", "Bearer second", "Bearer second"]
    assert "Authorization" not in agent._CRM_CLIENT.headers
    client = agent._CRM_CLIENT
    agent._close_crm_client()
    assert client.is_closed and agent._CRM_CLIENT is None


@pytest.mark.parametrize("status,retryable", [(401, False), (409, False), (503, True), (520, True)])
def test_http_failures_keep_existing_retry_contract(crm_server, status, retryable):
    server, url = crm_server
    with pytest.raises(agent.CRMRequestError) as caught:
        agent._post_json(f"{url}/status/{status}", "test", {})
    assert caught.value.retryable is retryable
    assert caught.value.retry_after == (7 if status == 503 else None)
    assert f"HTTP {status}" in str(caught.value)
    assert len(server.received) == 1  # No hidden automatic replay of POST.


def test_transport_failure_closes_client_and_reconnects_on_next_attempt(monkeypatch):
    agent._close_crm_client()
    clients = []
    def factory():
        fail = not clients
        def handle(request):
            if fail:
                raise httpx.ReadTimeout("disconnected", request=request)
            return httpx.Response(200, json={"ok": True})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client
    monkeypatch.setattr(agent, "_new_crm_client", factory)
    try:
        with pytest.raises(agent.CRMRequestError) as caught:
            agent._post_json("https://crm.example/claim", "test", {})
        assert caught.value.retryable
        assert clients[0].is_closed and agent._CRM_CLIENT is None
        assert agent._post_json("https://crm.example/claim", "test", {}) == {"ok": True}
        assert len(clients) == 2
    finally:
        agent._close_crm_client()
