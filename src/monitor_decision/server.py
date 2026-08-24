from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .agentic import AgentConfig, decide
from .models import DecisionRequest


class DecisionHandler(BaseHTTPRequestHandler):
    server_version = "MireyeMonitorDecision/0.1"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/decide":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            mode = str(payload.get("decision_mode", "rules")).lower()
            provider = str(payload.get("llm_provider", "auto")).lower()
            response = decide(
                DecisionRequest.from_dict(payload),
                AgentConfig.from_env(enabled=mode == "agentic", provider=provider),
            )
        except KeyError as exc:
            self._json(400, {"error": "bad_request", "detail": f"missing key: {exc}"})
            return
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": "bad_request", "detail": str(exc)})
            return
        self._json(200, response)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), DecisionHandler)
    print(f"Decision service listening on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
