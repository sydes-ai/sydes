"""A loopback relay between AACR-Bench's judge and the OpenAI API.

AACR's `evaluation/judge.py` calls an OpenAI-compatible endpoint with
`temperature`, `top_p`, `max_tokens` and `extra_body.chat_template_kwargs`.
OpenAI's reasoning models reject `chat_template_kwargs` (and do not take the
sampling parameters), so the relay removes `chat_template_kwargs`,
`temperature` and `top_p` and renames `max_tokens` to `max_completion_tokens`
-- the same rewrite OpenQodex's AACR relay applies, so the judge model sees the
same requests in both. Nothing else in a request or a response is touched, and
AACR's evaluator itself stays unchanged.

The evaluator gets a placeholder key; the real one stays in this process.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://api.openai.com/v1"
DROPPED = ("chat_template_kwargs", "temperature", "top_p")


def rewrite(body: dict) -> dict:
    body = {k: v for k, v in body.items() if k not in DROPPED}
    if "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    return body


class Relay:
    """Start with `start()`; `base_url` is what JUDGE_BASE_URL should point at."""

    def __init__(self, api_key: str, upstream: str = UPSTREAM) -> None:
        self.api_key, self.upstream = api_key, upstream.rstrip("/")
        self.counts = {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}
        self._lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the evaluator's console clean
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = rewrite(json.loads(self.rfile.read(length) or b"{}"))
                path = self.path[len("/v1"):] if self.path.startswith("/v1") else self.path
                request = urllib.request.Request(
                    relay.upstream + path, data=json.dumps(body).encode(), method="POST",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {relay.api_key}"})
                try:
                    with urllib.request.urlopen(request, timeout=600) as response:
                        status, payload = response.status, response.read()
                except urllib.error.HTTPError as error:
                    status, payload = error.code, error.read()
                relay._record(status, payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def _record(self, status: int, payload: bytes) -> None:
        with self._lock:
            self.counts["calls"] += 1
            if status >= 400:
                self.counts["errors"] += 1
                return
            try:
                usage = json.loads(payload).get("usage") or {}
            except ValueError:
                return
            self.counts["input_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.counts["output_tokens"] += int(usage.get("completion_tokens") or 0)

    def start(self) -> "Relay":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()


def from_env() -> Relay:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is required for the AACR judge relay")
    return Relay(key).start()
