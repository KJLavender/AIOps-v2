"""Prometheus registry + the small HTTP server every agent runs."""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

log = logging.getLogger("aiops.metrics")


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple], float] = {}
        self._kinds: dict[str, str] = {}

    def inc(self, name: str, amount: float = 1, **labels) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._kinds[name] = "counter"
            self._values[key] = self._values.get(key, 0) + amount

    def set(self, name: str, value: float, **labels) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._kinds[name] = "gauge"
            self._values[key] = float(value)

    def render(self) -> str:
        lines, seen = [], set()
        with self._lock:
            for (name, labels), value in sorted(self._values.items()):
                if name not in seen:
                    lines.append(f"# TYPE {name} {self._kinds[name]}")
                    seen.add(name)
                text = str(int(value)) if float(value).is_integer() else repr(value)
                label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_text}}} {text}" if label_text else f"{name} {text}")
        return "\n".join(lines) + "\n"


METRICS = Registry()


def serve(port: int, agent: str, routes: Optional[dict[str, Callable[[], tuple[str, bytes]]]] = None,
          healthy: Callable[[], bool] = lambda: True,
          post: Optional[Callable[[str, dict], tuple[int, str, bytes]]] = None) -> ThreadingHTTPServer:
    """/metrics and /healthz, plus optional extra GET routes -> (content type, body).

    post(path, query) answers POST requests -> (status, content type, body).
    """
    routes = routes or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                self._send(200, "text/plain; version=0.0.4", METRICS.render().encode())
            elif path == "/healthz":
                ok = healthy()
                self._send(200 if ok else 503, "application/json",
                           json.dumps({"agent": agent, "ok": ok}).encode())
            elif path in routes:
                ctype, body = routes[path]()
                self._send(200, ctype, body)
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if post is None:
                self._send(405, "text/plain", b"method not allowed")
                return
            path, _, query = self.path.partition("?")
            code, ctype, body = post(path, {k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self._send(code, ctype, body)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("%s agent HTTP on :%s", agent, port)
    return server


class Loop:
    """Run step() every interval seconds; a failing step never kills the agent."""

    def __init__(self, agent: str, interval: float) -> None:
        self.agent = agent
        self.interval = interval
        self.last_ok = time.time()

    def healthy(self) -> bool:
        return time.time() - self.last_ok < max(120.0, self.interval * 6)

    def run_forever(self, step: Callable[[], None]) -> None:
        while True:
            try:
                step()
                self.last_ok = time.time()
                METRICS.set("aiops_agent_last_loop_timestamp_seconds", self.last_ok, agent=self.agent)
            except Exception:
                log.exception("%s loop failed", self.agent)
                METRICS.inc("aiops_agent_loop_errors_total", agent=self.agent)
            time.sleep(self.interval)
