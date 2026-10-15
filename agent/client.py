import json
import time
import urllib.error
import urllib.request


class ProxyClient:
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

    def complete(self, messages, model: str, step_no: int, max_usd: float, spent_usd: float = 0) -> dict:
        return self._retry(
            "POST",
            "/v1/complete",
            {
                "token": self.token,
                "messages": messages,
                "tools": [],
                "model": model,
                "step_no": step_no,
                "max_usd": max_usd,
                "spent_usd": spent_usd,
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

    def faults(self, faults: list[str]) -> dict:
        status, body = self._send("POST", "/v1/faults", {"faults": faults})
        body["status"] = status
        return body

    def _retry(self, method: str, path: str, payload: dict) -> dict:
        body = {"error": "token"}
        status = 403
        for _ in range(60):
            status, body = self._send(method, path, payload)
            if status != 403:
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
