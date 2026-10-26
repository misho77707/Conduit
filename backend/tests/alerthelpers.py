"""A local HTTP server standing in for Telegram, Slack and webhook endpoints: it records what it receives."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Sink:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reply = (200, b'{"ok": true}')
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sink.requests.append({"path": self.path, "headers": dict(self.headers), "raw": body,
                                      "json": json.loads(body) if body else None})
                code, out = sink.reply
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, n: int, timeout: float = 5.0) -> list[dict]:
        """The payloads once at least n requests arrived (a job's hook sends just after the job is marked done)."""
        end = time.time() + timeout
        while time.time() < end and len(self.requests) < n:
            time.sleep(0.05)
        return [r["json"] for r in self.requests]

    def take(self) -> list[dict]:
        out = [r["json"] for r in self.requests]
        self.requests.clear()
        return out

    def close(self) -> None:
        self.server.shutdown()
