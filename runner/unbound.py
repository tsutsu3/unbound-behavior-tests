"""Drive Unbound: build a config for a case, run the binaries, query them.

This is the only place that starts an external process.  What a case says
should happen to the *upstream* side is observed in `runner.upstream`; here
is everything on the Unbound side of the wire:

* the boilerplate wrapped around every case config, and where the case's
  own text and its `files:` land on disk,
* running `unbound-checkconf`,
* running and stopping `unbound -d`, and collecting its stderr,
* sending the case's `query` to it.
"""

import dataclasses
import os
import pathlib
import signal
import subprocess
import threading
import time
from typing import Self

import dns.exception
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype

#: Port Unbound itself listens on while a case runs.  Cases never mention
#: it; only the upstream ports they care about are fixed by the case.
UNBOUND_PORT = 15353
UNBOUND_ADDR = "127.0.0.1"

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


def build_config(case_config: str) -> str:
    """Boilerplate first, then the case verbatim.

    The case text is appended rather than merged, so a case may either add
    `server:` options (they land in the boilerplate's server clause) or open
    its own top level clause such as `forward-zone:`.
    """
    body = BOILERPLATE.format(addr=UNBOUND_ADDR, port=UNBOUND_PORT)
    return (
        body + case_config if case_config.endswith("\n") else body + case_config + "\n"
    )


def write_config(
    tmpdir: pathlib.Path,
    case_config: str,
    files: dict[str, str] | None = None,
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
    path.write_text(build_config(case_config), encoding="utf-8")
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

    def __init__(self, conf: pathlib.Path):
        self.conf = conf
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
                dns.query.udp(probe, UNBOUND_ADDR, port=UNBOUND_PORT, timeout=0.5)
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


def send_query(spec: str, timeout: float = 3.0) -> dns.message.Message | None:
    name, rrtype = parse_query(spec)
    msg = dns.message.make_query(name, dns.rdatatype.from_text(rrtype))
    try:
        return dns.query.udp(msg, UNBOUND_ADDR, port=UNBOUND_PORT, timeout=timeout)
    except (OSError, dns.exception.DNSException):
        # No answer is a normal outcome: a run-and-observe case only needs
        # Unbound to send the query onwards, and the fake upstream never
        # replies, so this times out by design.  The caller decides whether
        # that matters.
        return None
