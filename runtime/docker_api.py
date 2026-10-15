import json
import socket
import http.client


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.socket_path)


def docker_request(method: str, path: str, body: dict | None = None, socket_path: str = "/var/run/docker.sock"):
    connection = UnixHTTPConnection(socket_path)
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    if not raw:
        return response.status, None
    try:
        return response.status, json.loads(raw.decode())
    except json.JSONDecodeError:
        return response.status, raw.decode(errors="replace")


def create_agent_container(name: str, host_repo: str, network: str, env: list[str], socket_path: str = "/var/run/docker.sock") -> str:
    body = {
        "Image": "swarm-agent",
        "User": "65534:65534",
        "Tty": True,
        "WorkingDir": "/src",
        "Env": env,
        "Cmd": ["python", "-m", "agent"],
        "HostConfig": {
            "NetworkMode": network,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Memory": 536870912,
            "NanoCpus": 500000000,
            "PidsLimit": 64,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "Binds": [f"{host_repo}:/src:ro"],
        },
    }
    docker_request("DELETE", f"/containers/{name}?force=1", socket_path=socket_path)
    status, payload = docker_request("POST", f"/containers/create?name={name}", body, socket_path)
    if status not in (200, 201):
        raise RuntimeError(f"docker create {status} {payload}")
    return payload["Id"]


def start_container(container_id: str, socket_path: str = "/var/run/docker.sock") -> None:
    status, payload = docker_request("POST", f"/containers/{container_id}/start", socket_path=socket_path)
    if status not in (204, 304):
        raise RuntimeError(f"docker start {status} {payload}")


def kill_container(container_id: str, socket_path: str = "/var/run/docker.sock") -> None:
    docker_request("POST", f"/containers/{container_id}/kill", socket_path=socket_path)


def inspect_container(container_id: str, socket_path: str = "/var/run/docker.sock") -> dict:
    status, payload = docker_request("GET", f"/containers/{container_id}/json", socket_path=socket_path)
    if status != 200:
        raise RuntimeError(f"docker inspect {status} {payload}")
    return payload


def remove_container(container_id: str, socket_path: str = "/var/run/docker.sock") -> None:
    docker_request("DELETE", f"/containers/{container_id}?force=1", socket_path=socket_path)


def container_ip(info: dict, network: str) -> str:
    networks = info["NetworkSettings"]["Networks"]
    if network in networks and networks[network].get("IPAddress"):
        return networks[network]["IPAddress"]
    for settings in networks.values():
        if settings.get("IPAddress"):
            return settings["IPAddress"]
    raise RuntimeError("container has no ip")
