"""Unix-socket control channel between the root worker and the user's menu bar app.

Protocol: one line in ("status" | "stop"), one JSON line out. The socket is owned by the
controlling user with mode 0600, so only that user can talk to the root worker.
"""
import json
import os
import socket
import threading


def sock_path(uid):
    return os.path.join(os.environ.get("REHORNDIS_SOCK_DIR", "/var/run"), f"rehorndis-{uid}.sock")


def request(uid, cmd, timeout=1.0):
    """Client side: send a command, return the decoded reply, or None if no worker is listening."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path(uid))
        s.sendall(cmd.encode() + b"\n")
        return json.loads(s.makefile().readline())
    except (OSError, ValueError):
        return None
    finally:
        s.close()


class ControlServer:
    def __init__(self, uid, stop):
        self.uid, self.stop, self.path = uid, stop, sock_path(uid)
        self.info = {"state": "starting"}
        self.stats_fn = None
        self._sock = None

    def update(self, **kw):
        self.info.update(kw)

    def start(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        if os.geteuid() == 0:
            os.chown(self.path, self.uid, -1)
        s.listen(4)
        self._sock = s
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # closed
            with conn:
                try:
                    conn.settimeout(2)
                    cmd = conn.makefile().readline().strip()
                    if cmd == "stop":
                        self.stop.set()
                        self.info["state"] = "stopping"
                    reply = dict(self.info, **(self.stats_fn() if self.stats_fn else {}))
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass

    def close(self):
        if self._sock:
            self._sock.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
