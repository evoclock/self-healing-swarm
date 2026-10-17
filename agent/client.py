import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ProxyClient:
    # 0.5 s per poll, about 90 seconds. A same-generation holder is not taken over when this ends.
    poll_limit = 180

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def whoami(self) -> str:
        status, body = self._send("GET", "/whoami", None)
        if status != 200:
            raise RuntimeError(body)
        return body["source_ip"]

    def tool(self, name: str, args: dict, step_no: int) -> dict:
        return self._retry(
            "POST",
            "/v1/tool",
            {"token": self.token, "name": name, "args": args, "step_no": step_no},
        )

    def complete(self, messages, model: str, step_no: int) -> dict:
        """No budget fields. The proxy reads the cap from the run and charges usage.cost."""
        return self._retry(
            "POST",
            "/v1/complete",
            {
                "token": self.token,
                "messages": messages,
                "tools": [],
                "model": model,
                "step_no": step_no,
            },
        )

    def state(self, state: str, artifact: str) -> dict:
        status, body = self._send(
            "POST",
            "/v1/state",
            {"token": self.token, "state": state, "artifact": artifact},
        )
        body["status"] = status
        return body

    def event(self, event: str, detail: dict) -> dict:
        status, body = self._send(
            "POST",
            "/v1/event",
            {"token": self.token, "event": event, "detail": detail},
        )
        body["status"] = status
        return body

    def heartbeat(self) -> dict:
        """Renews the run's lease while the agent works locally (py.exec, sheet program)."""
        status, body = self._send("POST", "/v1/heartbeat", {"token": self.token})
        body["status"] = status
        return body

    def forward(self, method: str, target: str) -> dict:
        """Use the proxy as a forward proxy (CONNECT host:port or an absolute URL). The proxy refuses these."""
        parsed = urllib.parse.urlsplit(self.base_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
        try:
            conn.request(method, target, headers={"X-Run-Token": self.token})
            response = conn.getresponse()
            raw = response.read().decode()
            status = response.status
        finally:
            conn.close()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": raw}
        body["status"] = status
        return body

    def faults(self, faults: list[str]) -> dict:
        status, body = self._send("POST", "/v1/faults", {"faults": faults})
        body["status"] = status
        return body

    def _retry(self, method: str, path: str, payload: dict) -> dict:
        """Wait out 403 while the token binds, and poll 202 while another caller holds the step."""
        body = {"error": "token"}
        status = 403
        unbound = 0
        waiting = 0
        while unbound < 60 and waiting < self.poll_limit:
            status, body = self._send(method, path, payload)
            if status == 403:
                unbound += 1
            elif status == 202:
                waiting += 1
            else:
                break
            time.sleep(0.5)
        body["status"] = status
        return body

    def _send(self, method: str, path: str, payload) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"error": raw}
            return exc.code, body
