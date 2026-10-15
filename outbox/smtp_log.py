import json
import socket
import threading
from pathlib import Path


class SmtpLog(list):
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    super().append(json.loads(line))

    def append(self, item):
        super().append(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(item) + "\n")


def smtp_send(host: str, port: int, message_id: str, recipient: str, body: str) -> None:
    with socket.create_connection((host, port), 5) as sock:
        file = sock.makefile("rwb")
        _read(file)
        _write(file, b"HELO drill\r\n")
        _read(file)
        _write(file, b"MAIL FROM:<outbox@swarm>\r\n")
        _read(file)
        _write(file, f"RCPT TO:<{recipient}>\r\n".encode())
        _read(file)
        _write(file, b"DATA\r\n")
        _read(file)
        payload = f"Message-ID: {message_id}\r\nTo: {recipient}\r\n\r\n{body}\r\n.\r\n"
        _write(file, payload.encode())
        _read(file)
        _write(file, b"QUIT\r\n")


def serve_smtp(path: Path, host: str = "127.0.0.1", port: int = 2525) -> tuple[socket.socket, threading.Thread]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)
    thread = threading.Thread(target=_accept, args=(server, Path(path)), daemon=True)
    thread.start()
    return server, thread


def _accept(server: socket.socket, path: Path) -> None:
    while True:
        try:
            connection, _addr = server.accept()
        except OSError:
            return
        threading.Thread(target=_session, args=(connection, path), daemon=True).start()


def _session(connection: socket.socket, path: Path) -> None:
    file = connection.makefile("rwb")
    _write(file, b"220 drill smtp\r\n")
    message_id = ""
    recipient = ""
    collecting = False
    lines = []
    while True:
        raw = file.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        upper = line.upper()
        if collecting:
            if line == ".":
                body = "\n".join(lines)
                SmtpLog(path).append({"message_id": message_id, "to": recipient, "body": body})
                _write(file, b"250 queued\r\n")
                collecting = False
                lines = []
            else:
                if line.lower().startswith("message-id:"):
                    message_id = line.split(":", 1)[1].strip()
                lines.append(line)
            continue
        if upper.startswith("EHLO") or upper.startswith("HELO"):
            _write(file, b"250 drill\r\n")
        elif upper.startswith("MAIL FROM"):
            _write(file, b"250 ok\r\n")
        elif upper.startswith("RCPT TO"):
            recipient = line.split(":", 1)[-1].strip().strip("<>")
            _write(file, b"250 ok\r\n")
        elif upper == "DATA":
            collecting = True
            _write(file, b"354 end with dot\r\n")
        elif upper == "QUIT":
            _write(file, b"221 bye\r\n")
            break
        else:
            _write(file, b"250 ok\r\n")
    connection.close()


def _write(file, data: bytes) -> None:
    file.write(data)
    file.flush()


def _read(file) -> str:
    return file.readline().decode(errors="replace")
