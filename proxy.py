#!/usr/bin/env python3
"""OpenAI-compatible reverse proxy for the DeepSeek API that turns thinking off.

MetaTube (metatube-sdk-go) can only be configured with an api url, key and model, so it cannot
send DeepSeek's `thinking` parameter. Point MetaTube's "OpenAI api url" at this proxy instead:
every chat completion request gets `"thinking": {"type": "disabled"}` added (unless the client
already set it) and is forwarded to DeepSeek unchanged otherwise.

- The caller's Authorization header is passed through; the proxy stores no API key.
- Token usage of every request is appended to a JSONL log (no prompt/response content).
- Standard library only (runs on the stock python3 of Ubuntu 22.04).
"""
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8765"))
UPSTREAM = os.environ.get("UPSTREAM", "https://api.deepseek.com").rstrip("/")
# "disabled": force thinking off; "passthrough": forward requests untouched.
THINKING = os.environ.get("THINKING", "disabled")
USAGE_LOG = os.environ.get("USAGE_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.jsonl"))
TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "300"))

# Hop-by-hop headers must not be forwarded.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}

BEIJING = timezone(timedelta(hours=8))

log = logging.getLogger("deepseek-proxy")


def rewrite_body(body: dict) -> dict:
    if THINKING == "disabled" and "thinking" not in body:
        body["thinking"] = {"type": "disabled"}
    return body


def record_usage(entry: dict) -> None:
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("cannot write usage log: %s", e)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deepseek-proxy"

    def log_message(self, fmt, *args):  # route http.server access logs through logging
        log.info("%s %s", self.address_string(), fmt % args)

    def log_request(self, code="-", size="-"):
        if getattr(self, "path", None) != "/healthz":  # the Docker healthcheck hits it every 30s
            super().log_request(code, size)

    def _send(self, status: int, headers, body: bytes) -> None:
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        if self.path == "/healthz":
            return self._send(200, [("Content-Type", "text/plain")], b"ok\n")

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else None
        is_chat = self.command == "POST" and self.path.rstrip("/").endswith("/chat/completions")

        req_body, model, stream = None, None, False
        if is_chat and raw:
            try:
                req_body = rewrite_body(json.loads(raw))
                model, stream = req_body.get("model"), bool(req_body.get("stream"))
                raw = json.dumps(req_body).encode()
            except ValueError:
                pass  # not JSON: forward as-is and let DeepSeek reject it

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        upstream_req = urllib.request.Request(UPSTREAM + self.path, data=raw, method=self.command, headers=headers)
        started = time.time()
        try:
            resp = urllib.request.urlopen(upstream_req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            resp = e  # relay DeepSeek's error status and body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.error("upstream error: %s", e)
            body = json.dumps({"error": {"message": f"deepseek-proxy: upstream error: {e}"}}).encode()
            return self._send(502, [("Content-Type", "application/json")], body)

        status = resp.status if hasattr(resp, "status") else resp.code
        if stream and status == 200:
            return self._stream(resp, status)

        body = resp.read()
        self._send(status, resp.headers.items(), body)

        if is_chat:
            entry = {"ts": datetime.now(BEIJING).isoformat(timespec="seconds"), "client": self.client_address[0],
                     "model": model, "status": status, "sec": round(time.time() - started, 2),
                     "thinking": (req_body or {}).get("thinking", {}).get("type") if req_body else None}
            try:
                usage = json.loads(body).get("usage") or {}
                entry.update(prompt=usage.get("prompt_tokens"), cache_hit=usage.get("prompt_cache_hit_tokens"),
                             cache_miss=usage.get("prompt_cache_miss_tokens"),
                             completion=usage.get("completion_tokens"),
                             reasoning=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
            except ValueError:
                pass
            record_usage(entry)

    def _stream(self, resp, status: int) -> None:
        # Streaming responses are relayed chunk by chunk (MetaTube does not stream; kept for other clients).
        self.send_response(status)
        for k, v in resp.headers.items():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    do_GET = do_POST = do_PUT = do_DELETE = _proxy


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("listening on %s:%d -> %s (thinking=%s, usage log %s)", LISTEN_HOST, LISTEN_PORT, UPSTREAM, THINKING, USAGE_LOG)
    server.serve_forever()


if __name__ == "__main__":
    main()
