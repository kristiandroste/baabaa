"""The terminal client's connection to the server: HTTP over the local Unix socket, plus the event stream."""

import http.client
import json
import os
import queue
import socket
import threading

from ..paths import Paths


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 600):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or Paths()
        self.socket = str(self.paths.socket)
        self.token = None
        key_path = self.paths.root / "local.key"
        self.local_key = key_path.read_text().strip() if key_path.exists() else ""
        self.events: queue.Queue = queue.Queue()
        self._stream_thread = None
        self._stop = threading.Event()

    def available(self) -> bool:
        return os.path.exists(self.socket)

    def request(self, method: str, path: str, body=None, raw: bool = False):
        conn = UnixConnection(self.socket)
        headers = {"X-Baabaa-Client": "terminal", "X-Baabaa-Local-Key": self.local_key}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        except OSError as exc:
            raise ApiError(0, f"baabaa is not running ({exc}). Start it with: baabaa start") from exc
        finally:
            conn.close()
        if raw:
            if resp.status >= 400:
                raise ApiError(resp.status, payload.decode(errors="replace")[:300])
            return payload, resp.getheader("Content-Disposition") or ""
        try:
            out = json.loads(payload) if payload else {}
        except ValueError:
            out = {"error": payload.decode(errors="replace")[:300]}
        if resp.status >= 400:
            raise ApiError(resp.status, out.get("error") or f"HTTP {resp.status}")
        return out

    get = lambda self, p: self.request("GET", p)  # noqa: E731
    post = lambda self, p, b=None: self.request("POST", p, b if b is not None else {})  # noqa: E731
    patch = lambda self, p, b: self.request("PATCH", p, b)  # noqa: E731

    # session ---------------------------------------------------------------------------------------
    def _token_file(self) -> str:
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        d = os.path.join(cfg, "baabaa")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return os.path.join(d, "terminal-session.json")

    def saved_sessions(self) -> dict:
        try:
            with open(self._token_file()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_session(self, account_name: str, token: str) -> None:
        data = self.saved_sessions()
        data[account_name] = token
        data["_last"] = account_name
        fd = os.open(self._token_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)

    def resume(self, account_name: str | None) -> dict | None:
        data = self.saved_sessions()
        name = account_name or data.get("_last")
        if not name or name not in data:
            return None
        self.token = data[name]
        try:
            return self.get("/api/me")
        except ApiError:
            self.token = None
            return None

    def login(self, account: dict, password: str | None) -> dict:
        out = self.post("/api/login", {"account_id": account["id"], "password": password})
        self.token = out["token"]
        self.save_session(account["name"], self.token)
        return self.get("/api/me")

    # events ------------------------------------------------------------------------------------------
    def start_events(self) -> None:
        self._stop.clear()
        self._stream_thread = threading.Thread(target=self._stream, daemon=True)
        self._stream_thread.start()

    def _stream(self) -> None:
        while not self._stop.is_set():
            try:
                conn = UnixConnection(self.socket, timeout=60)
                conn.request("GET", "/api/events", headers={"Authorization": f"Bearer {self.token}",
                                                             "X-Baabaa-Client": "terminal"})
                resp = conn.getresponse()
                if resp.status != 200:
                    self.events.put(("error", {"text": f"event stream refused ({resp.status})"}))
                    return
                self.events.put(("connected", {}))
                event, data = None, []
                while not self._stop.is_set():
                    line = resp.fp.readline()
                    if not line:
                        break
                    line = line.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data.append(line[6:])
                    elif line == "" and event:
                        try:
                            self.events.put((event, json.loads("\n".join(data))))
                        except ValueError:
                            pass
                        event, data = None, []
            except OSError:
                pass
            self.events.put(("disconnected", {}))
            self._stop.wait(2)

    def stop(self) -> None:
        self._stop.set()
