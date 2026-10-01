"""Drive Unbound: build a config for a case, run the binaries, query them.

This is the only place that starts an external process.  What a case says
should happen to the *upstream* side is observed in `runner.upstream`; here
is everything on the Unbound side of the wire:

* the boilerplate wrapped around every case config, and where the case's
  own text and its `files:` land on disk,
* running `unbound-checkconf`,
* running and stopping `unbound -d`, and collecting its stderr,
* sending the case's `query` to it, with dnspython for the checks and with
  `dig` for the output the book includes,
* pointing the system stub resolver at it and running `getent ahosts`.
"""

import contextlib
import dataclasses
import os
import pathlib
import signal
import subprocess
import threading
import time
from typing import Self

import dns.exception
import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype

#: Port Unbound itself listens on while a case runs.  Cases never mention
#: it; only the upstream ports they care about are fixed by the case.
UNBOUND_PORT = 15353
UNBOUND_ADDR = "127.0.0.1"

#: Port used instead by run-and-resolve.  The stub resolver in glibc reads
#: `nameserver` lines from /etc/resolv.conf, which carry an address but no
#: port, so Unbound has to be on 53 for `getent` to reach it.
STUB_PORT = 53
RESOLV_CONF = pathlib.Path("/etc/resolv.conf")

CHECKCONF = os.environ.get("UNBOUND_CHECKCONF", "unbound-checkconf")
UNBOUND = os.environ.get("UNBOUND", "unbound")

#: Boilerplate wrapped around every case config.  Kept as small as it can be
#: while still letting Unbound run in a container as root and forward to a
#: loopback upstream:
#:
#: * `do-not-query-localhost: no` -- the default is yes, which would drop
#:   every forward to 127.0.0.1 before a packet was ever sent.
#: * `username: ""` / `chroot: ""` -- no privilege drop, no chroot.
#: * `use-syslog: no` with no logfile -- diagnostics go to stderr, where the
#:   runner can compare them against `stderr_contains`.
BOILERPLATE = """\
server:
    verbosity: 1
    interface: {addr}@{port}
    port: {port}
    access-control: 127.0.0.0/8 allow
    access-control: ::1 allow
    do-not-query-localhost: no
    do-daemonize: no
    username: ""
    chroot: ""
    directory: ""
    pidfile: ""
    use-syslog: no
    num-threads: 1
    so-reuseport: no
"""


#: The boilerplate for cases with `expect.client`: no access-control line, so
#: Unbound's built-in defaults decide, and every address is listened on so a
#: query can arrive from somewhere other than 127.0.0.1.
BOILERPLATE_OPEN = """\
server:
    verbosity: 1
    interface: 0.0.0.0@{port}
    interface: ::1@{port}
    port: {port}
    do-not-query-localhost: no
    do-daemonize: no
    username: ""
    chroot: ""
    directory: ""
    pidfile: ""
    use-syslog: no
    num-threads: 1
    so-reuseport: no
"""


def non_loopback_address() -> str:
    """The container's own IPv4 address on its default route.

    Nothing is sent: connecting a UDP socket only makes the kernel pick the
    source address it would use.  A query sent to this address from this host
    also carries it as its source, which is what a `client: non-loopback`
    case needs -- a client that is not in 127.0.0.0/8.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 53))
        return s.getsockname()[0]


def client_address(client: str | None) -> str:
    """Where a case's query is sent, for its `expect.client`."""
    if client == "non-loopback":
        return non_loopback_address()
    if client == "loopback6":
        return "::1"
    return UNBOUND_ADDR


def build_config(
    case_config: str, port: int = UNBOUND_PORT, client: str | None = None
) -> str:
    """Boilerplate first, then the case verbatim.

    The case text is appended rather than merged, so a case may either add
    `server:` options (they land in the boilerplate's server clause) or open
    its own top level clause such as `forward-zone:`.
    """
    template = BOILERPLATE_OPEN if client else BOILERPLATE
    body = template.format(addr=UNBOUND_ADDR, port=port)
    return (
        body + case_config if case_config.endswith("\n") else body + case_config + "\n"
    )


def write_config(
    tmpdir: pathlib.Path,
    case_config: str,
    files: dict[str, str] | None = None,
    port: int = UNBOUND_PORT,
    client: str | None = None,
) -> pathlib.Path:
    """Write the generated config, plus any extra files the case declared.

    Everything lands in one directory, and the binaries are run with that
    directory as their working directory (see `run_checkconf` and
    `UnboundProcess`), so a case can write `include: "common.conf"` without
    knowing the temporary directory's name.  Unbound resolves a relative
    include against the current directory: `config_start_include()` in
    util/configlexer.lex just calls `fopen(filename, "r")`.
    """
    for name, content in (files or {}).items():
        extra = tmpdir / name
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text(content, encoding="utf-8")
    path = tmpdir / "unbound.conf"
    path.write_text(build_config(case_config, port, client), encoding="utf-8")
    return path


@dataclasses.dataclass
class Run:
    exit_code: int
    stdout: str
    stderr: str


def run_checkconf(conf: pathlib.Path) -> Run:
    p = subprocess.run(
        [CHECKCONF, conf.name],
        cwd=conf.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return Run(p.returncode, p.stdout, p.stderr)


class UnboundProcess:
    """Run `unbound -d` in the foreground and collect its stderr."""

    def __init__(self, conf: pathlib.Path, port: int = UNBOUND_PORT):
        self.conf = conf
        self.port = port
        self.proc: subprocess.Popen | None = None
        self._err: list[str] = []
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self.proc = subprocess.Popen(
            [UNBOUND, "-d", "-c", self.conf.name],
            cwd=self.conf.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self._thread:
            self._thread.join(timeout=2)

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self._err.append(line)

    @property
    def stderr(self) -> str:
        return "".join(self._err)

    def wait_exit(self, timeout: float) -> int | None:
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Poll until the daemon answers on its own port, or it has died."""
        deadline = time.monotonic() + timeout
        probe = dns.message.make_query("ready.invalid.", dns.rdatatype.A)
        while time.monotonic() < deadline:
            assert self.proc is not None
            if self.proc.poll() is not None:
                return False
            try:
                dns.query.udp(probe, UNBOUND_ADDR, port=self.port, timeout=0.5)
                return True
            except (OSError, dns.exception.DNSException):
                # Not up yet: the port is refused, or the query times out.
                time.sleep(0.1)
        return False


def parse_query(spec: str) -> tuple[str, str]:
    """`"example.com. A"` -> ("example.com.", "A")."""
    parts = spec.split()
    if len(parts) != 2:
        raise ValueError(f"query must be '<name> <type>', got {spec!r}")
    return parts[0], parts[1]


def send_query(
    spec: str,
    timeout: float = 3.0,
    flags: tuple[str, ...] = (),
    edns: bool = False,
    addr: str = UNBOUND_ADDR,
) -> dns.message.Message | None:
    """Send the case's query; `flags` are from cases.QUERY_FLAGS.

    Without flags the query is what it always was: no EDNS, only RD.  `do`
    needs EDNS; `edns` asks for EDNS without DO, which a case uses to get an
    Extended DNS Error back.
    """
    name, rrtype = parse_query(spec)
    msg = dns.message.make_query(
        name,
        dns.rdatatype.from_text(rrtype),
        use_edns=0 if (edns or "do" in flags) else None,
        want_dnssec="do" in flags,
    )
    if "cd" in flags:
        msg.flags |= dns.flags.CD
    if "ad" in flags:
        msg.flags |= dns.flags.AD
    try:
        return dns.query.udp(msg, addr, port=UNBOUND_PORT, timeout=timeout)
    except (OSError, dns.exception.DNSException):
        # No answer is a normal outcome: a run-and-observe case only needs
        # Unbound to send the query onwards, and the fake upstream never
        # replies, so this times out by design.  The caller decides whether
        # that matters.
        return None


#: `dig` options for the output the book includes.  +nocmd drops the banner
#: that names the dig version and the harness's port; +nostats drops the
#: query time, the date and the server line.  Everything else is dig's
#: default output, so it is what a reader who runs
#: `dig @127.0.0.1 <name> <type>` sees, apart from the message id.
DIG_OPTIONS = ("+nocmd", "+nostats")


def dig_flags(flags: tuple[str, ...] | None) -> list[str]:
    """dig options that send the same flags as `send_query(flags=...)`.

    dig sets the AD bit by default; a case that lists its query flags and
    leaves `ad` out means a query without it, so that default is turned off.
    A case without `query_flags` keeps dig's defaults.
    """
    if flags is None:
        return []
    opts = ["+adflag" if "ad" in flags else "+noadflag"]
    if "do" in flags:
        opts.append("+dnssec")
    if "cd" in flags:
        opts.append("+cdflag")
    return opts


def dig_text(
    spec: str,
    port: int = UNBOUND_PORT,
    flags: tuple[str, ...] | None = None,
    addr: str = UNBOUND_ADDR,
) -> str:
    """Run `dig` against the daemon and return its output unchanged."""
    name, rrtype = parse_query(spec)
    p = subprocess.run(
        [
            "dig",
            f"@{addr}",
            "-p",
            str(port),
            *DIG_OPTIONS,
            *dig_flags(flags),
            name,
            rrtype,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return p.stdout


@contextlib.contextmanager
def stub_resolver_pointed_at_unbound():
    """Point glibc's stub resolver at the daemon, and put it back afterwards.

    Only the container's own /etc/resolv.conf is touched.  attempts:1 keeps
    a failing lookup from retrying for half a minute.
    """
    saved = RESOLV_CONF.read_text(encoding="utf-8")
    RESOLV_CONF.write_text(
        f"nameserver {UNBOUND_ADDR}\noptions attempts:1 timeout:2\n",
        encoding="utf-8",
    )
    try:
        yield
    finally:
        RESOLV_CONF.write_text(saved, encoding="utf-8")


def getent_ahosts(name: str) -> Run:
    """`getent ahosts <name>`: getaddrinfo() through glibc's stub resolver."""
    p = subprocess.run(
        ["getent", "ahosts", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return Run(p.returncode, p.stdout, p.stderr)
