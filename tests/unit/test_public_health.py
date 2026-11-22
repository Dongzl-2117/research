"""值班时通过正式域名查实际可读性。只连测试进程的回环 HTTP 服务。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from researchlib import health, public_health


def test_cli_checks_public_health_for_managed_installation(tmp_path, monkeypatch, capsys):
    requests = []
    reply = [200, b'{"status":"ok","commit":"current"}']

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Accept"), self.headers.get("User-Agent")))
            self.send_response(reply[0])
            self.end_headers()
            self.wfile.write(reply[1])

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv("DATA_DIR", str(tmp_path))
            monkeypatch.setenv("PUBLIC_BASE_URL", f"http://127.0.0.1:{server.server_port}/")
            monkeypatch.setenv("GIT_SHA", "current")
            # 本地开发通常也设 PUBLIC_BASE_URL；没有自动部署记录时不访问该域名。
            assert health.main([]) == 0 and requests == []
            capsys.readouterr()
            (tmp_path / "status").mkdir(exist_ok=True)
            (tmp_path / "status" / ".deploy-last").write_text("current ok\n")

            for code, body, message in [
                (530, b"error code: 1033", "HTTP 530"),
                (200, b"<html>login</html>", "未返回"),
                (200, b'{"status":"down","commit":"current"}', "未就绪"),
                (200, b'{"status":"ok","commit":"old"}', "old"),
                (200, b'{"status":"ok","commit":"current"}', "没有要处理的"),
            ]:
                reply[:] = [code, body]
                assert health.main([]) == int(message != "没有要处理的")
                assert message in capsys.readouterr().out
            assert requests == [("/health", "application/json", "ResearchLibrary-Health/1.0")] * 5
        finally:
            server.shutdown()
            thread.join()


def test_public_probe_has_a_timeout(monkeypatch):
    def timeout_request(_request, *, timeout):
        assert timeout == 5
        raise TimeoutError("timed out")

    monkeypatch.setattr(public_health, "urlopen", timeout_request)
    assert "从本站访问公网入口失败" in public_health.public_health_notice("https://example.test", "current")
