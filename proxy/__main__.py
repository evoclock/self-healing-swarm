import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from proxy.providers import Cassette, gateway_upstream
from proxy.service import ProxyContext, dispatch


def main() -> None:
    port = int(os.environ.get("PORT", "18894"))
    mode = os.environ.get("PROXY_MODE", "replay")
    root = Path(os.environ.get("RECORDINGS_DIR", "recordings/steps"))
    key = os.environ.get("PROVIDER_KEY", "")
    upstream = gateway_upstream(key) if mode == "record" else None
    ctx = ProxyContext(
        database_url=os.environ["DATABASE_URL"],
        cassette=Cassette(root),
        mode=mode,
        name=os.environ.get("PROXY_NAME", "proxy"),
        upstream=upstream,
        provider_tpm=int(os.environ["PROVIDER_TPM"]) if os.environ.get("PROVIDER_TPM") else None,
        provider_rpm=int(os.environ["PROVIDER_RPM"]) if os.environ.get("PROVIDER_RPM") else None,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._reply()

        def do_POST(self):
            self._reply()

        def do_CONNECT(self):
            self._reply()

        def _reply(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            status, payload = dispatch(ctx, self.command, self.path, raw, self.client_address[0], self.headers)
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if status == 429 and isinstance(payload, dict) and payload.get("retry_after_s") is not None:
                self.send_header("Retry-After", str(int(-(-float(payload["retry_after_s"]) // 1))))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"{ctx.name} listening on {port} mode {mode}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
