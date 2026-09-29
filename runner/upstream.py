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
what arrives is not inspected, only that it arrived here.
"""

import dataclasses
import queue
import socket
import threading
from typing import Self

import dns.exception
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset


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


@dataclasses.dataclass(frozen=True)
class Received:
    """One query as it reached a ServingUpstream."""

    qname: str
    qtype: str
    rd: bool
    proto: str

    def spec(self) -> str:
        return f"{self.qname} {self.qtype}"


class ServingUpstream:
    """A fake upstream that records what arrives and may answer it.

    FakeUpstream above only proves that *something* reached an address.  This
    one parses each query, records its name, type and RD bit, and answers
    with the first rule in `replies` whose qname / qtype match (a rule
    without them matches anything).  With no matching rule it stays silent,
    as FakeUpstream does, so Unbound times out.

    Over TCP it also keeps the first bytes of every connection, so a case can
    tell a TLS ClientHello (record type 0x16) from plain DNS over TCP (which
    starts with a two byte length).  A connection that starts with TLS is
    recorded and closed; nothing here speaks TLS.
    """

    def __init__(self, proto: str, addr: str, port: int, replies: list[dict]):
        self.proto = proto
        self.addr = addr
        self.port = port
        self.replies = replies
        self.received: list[Received] = []
        self.first_bytes: list[bytes] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def where(self) -> str:
        return f"{self.proto}://{self.addr}:{self.port}"

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

    def snapshot(self) -> list[Received]:
        with self._lock:
            return list(self.received)

    def _record(self, msg: dns.message.Message, proto: str) -> None:
        q = msg.question[0]
        with self._lock:
            self.received.append(
                Received(
                    qname=q.name.to_text().lower(),
                    qtype=dns.rdatatype.to_text(q.rdtype),
                    rd=bool(msg.flags & dns.flags.RD),
                    proto=proto,
                )
            )

    def _reply(self, query: dns.message.Message) -> dns.message.Message | None:
        q = query.question[0]
        qname = q.name.to_text().lower()
        qtype = dns.rdatatype.to_text(q.rdtype)
        for rule in self.replies:
            if "qname" in rule and rule["qname"].lower() != qname:
                continue
            if "qtype" in rule and rule["qtype"] != qtype:
                continue
            resp = dns.message.make_response(query)
            resp.set_rcode(dns.rcode.from_text(rule.get("rcode", "NOERROR")))
            if rule.get("aa"):
                resp.flags |= dns.flags.AA
            for section, key in (
                (resp.answer, "answer"),
                (resp.authority, "authority"),
                (resp.additional, "additional"),
            ):
                for text in rule.get(key, []):
                    rrset = dns.rrset.from_text(*_split_rr(text))
                    section.append(rrset)
            return resp
        return None

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                if self.proto == "udp":
                    data, peer = self._sock.recvfrom(4096)
                    self._handle_udp(data, peer)
                else:
                    conn, _ = self._sock.accept()
                    self._handle_tcp(conn)
            except TimeoutError:
                continue
            except OSError:
                return

    def _handle_udp(self, data: bytes, peer) -> None:
        try:
            query = dns.message.from_wire(data)
        except dns.exception.DNSException:
            return
        self._record(query, "udp")
        resp = self._reply(query)
        if resp is not None and self._sock is not None:
            self._sock.sendto(resp.to_wire(), peer)

    def _handle_tcp(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        try:
            head = conn.recv(2)
            with self._lock:
                self.first_bytes.append(head)
            if len(head) < 2 or head[0] == 0x16:
                return  # TLS, or nothing: record and hang up
            length = int.from_bytes(head, "big")
            data = b""
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    return
                data += chunk
            query = dns.message.from_wire(data)
            self._record(query, "tcp")
            resp = self._reply(query)
            if resp is not None:
                wire = resp.to_wire()
                conn.sendall(len(wire).to_bytes(2, "big") + wire)
        except (OSError, dns.exception.DNSException):
            return
        finally:
            conn.close()


def _split_rr(text: str) -> tuple:
    """`"example.com. 300 IN A 192.0.2.1"` -> args for dns.rrset.from_text."""
    name, ttl, rdclass, rdtype, *rdata = text.split()
    return (name, int(ttl), rdclass, rdtype, " ".join(rdata))
