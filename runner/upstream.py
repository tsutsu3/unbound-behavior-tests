"""Stand in for the server a `forward-zone` / `stub-zone` case points at.

A case says where Unbound should send, and this binds exactly that address
and port.  If a packet or connection arrives, the address was resolved the
way the case predicted.

Two decisions here carry the weight of the forward-zone results:

* The observation is made by *being* the upstream, not by inspecting the
  system.  Nothing greps `ss`: a UDP socket does not appear there until it
  sends, and parsing `ss` output is environment dependent.  Binding and
  waiting is deterministic and needs no network beyond loopback.

* The port comes from the case as a literal.  `forward-addr: 127.0.0.1@70000`
  is only interesting because 70000 is truncated to 4464; a dynamically
  allocated port could not observe that at all.

Nothing is ever sent back.  Unbound simply times out, which is enough --
what arrives is not inspected, only that it arrived here (plan section 4.4).
"""

import queue
import socket
import threading
from typing import Self


class FakeUpstream:
    """Bind the address a forward-zone case expects Unbound to send to."""

    def __init__(self, proto: str, addr: str, port: int):
        self.proto = proto
        self.addr = addr
        self.port = port
        self._hits: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        kind = socket.SOCK_DGRAM if self.proto == "udp" else socket.SOCK_STREAM
        family = socket.AF_INET6 if ":" in self.addr else socket.AF_INET
        sock = socket.socket(family, kind)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.addr, self.port))
        if self.proto == "tcp":
            sock.listen(8)
        sock.settimeout(0.2)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                if self.proto == "udp":
                    data, peer = self._sock.recvfrom(4096)
                    self._hits.put(f"udp {len(data)} bytes from {peer[0]}:{peer[1]}")
                else:
                    conn, peer = self._sock.accept()
                    self._hits.put(f"tcp connect from {peer[0]}:{peer[1]}")
                    conn.close()
            except TimeoutError:
                continue
            except OSError:
                return

    def wait(self, timeout: float) -> str | None:
        try:
            return self._hits.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def where(self) -> str:
        return f"{self.addr}:{self.port}/{self.proto}"
