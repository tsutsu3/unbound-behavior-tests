"""The single entry point for every case in cases/*.yaml.

There is deliberately one test function.  A case is data, not code: adding a
behaviour to the suite means adding a YAML file, never a test function.

`status` maps onto pytest as follows.

    verified      run it, compare, fail on any mismatch
    needs-test    skip, unless --run-unverified was given
    inconclusive  skip always; the case records a question, not an answer

Mismatches are reported by building the difference explicitly rather than by
leaning on assert introspection, so a failure says which case, which
expectation, what was expected and what actually happened.
"""

import contextlib
import pathlib
import re
import tempfile

import pytest

from runner import unbound, upstream
from runner.cases import Case, load_cases

CASES = load_cases()


class Options:
    """Command line options a runner may need."""

    def __init__(self, config):
        dig_out = config.getoption("--dig-out")
        self.dig_out = pathlib.Path(dig_out) if dig_out else None


class Mismatch(AssertionError):
    pass


class Report:
    """Collect every difference for a case, then raise once."""

    def __init__(self, case: Case):
        self.case = case
        self.diffs: list[str] = []
        self.context: list[str] = []

    def check(self, what: str, expected, actual) -> None:
        if expected != actual:
            self.diffs.append(
                f"  {what}\n    expected: {expected!r}\n    actual:   {actual!r}"
            )

    def check_contains(self, what: str, needle: str, haystack: str) -> None:
        if needle not in haystack:
            self.diffs.append(
                f"  {what}\n    expected to contain: {needle!r}\n"
                f"    actual:              {haystack.strip()[-2000:]!r}"
            )

    def check_not_contains(self, what: str, needle: str, haystack: str) -> None:
        if needle in haystack:
            self.diffs.append(
                f"  {what}\n    expected not to contain: {needle!r}\n"
                f"    actual:                  {haystack.strip()[-2000:]!r}"
            )

    def note(self, line: str) -> None:
        self.context.append(line)

    def finish(self) -> None:
        if not self.diffs:
            return
        head = (
            f"case {self.case.id} ({self.case.path.name}) did not behave as "
            f"predicted from the source.\n"
            f"  title:  {self.case.title.en}\n"
            f"  source: {self.case.source.en}\n"
        )
        ctx = "".join(f"  {line}\n" for line in self.context)
        raise Mismatch(head + ctx + "\n" + "\n".join(self.diffs))


def _skip_reason(case: Case, run_unverified: bool) -> str | None:
    if case.status == "verified":
        return None
    if case.status == "needs-test":
        if run_unverified:
            return None
        return (
            "status: needs-test -- the expected value is not confirmed yet; "
            "rerun with --run-unverified to judge it"
        )
    return "status: inconclusive -- the case records an open question"


def _run_checkconf_case(case: Case, tmp: pathlib.Path, report: Report, opts) -> None:
    conf = unbound.write_config(tmp, case.config, case.files)
    result = unbound.run_checkconf(conf)
    report.note(f"unbound-checkconf exit={result.exit_code}")
    if "exit" in case.expect:
        report.check(
            "unbound-checkconf exit code", case.expect["exit"], result.exit_code
        )
    if "stderr_contains" in case.expect:
        report.check_contains(
            "unbound-checkconf stderr",
            case.expect["stderr_contains"],
            result.stderr + result.stdout,
        )
    if "stderr_not_contains" in case.expect:
        report.check_not_contains(
            "unbound-checkconf stderr",
            case.expect["stderr_not_contains"],
            result.stderr + result.stdout,
        )


def _checkconf_gate(
    case: Case, tmp: pathlib.Path, report: Report, port: int = unbound.UNBOUND_PORT
):
    """Every runtime case also states what checkconf did with the config."""
    conf = unbound.write_config(tmp, case.config, case.files, port)
    result = unbound.run_checkconf(conf)
    report.note(f"unbound-checkconf exit={result.exit_code}")
    if "checkconf_exit" in case.expect:
        report.check(
            "unbound-checkconf exit code",
            case.expect["checkconf_exit"],
            result.exit_code,
        )
    return conf, result


def _run_observe_case(case: Case, tmp: pathlib.Path, report: Report, opts) -> None:
    conf, _ = _checkconf_gate(case, tmp, report)
    listen = case.expect["listen"]
    addr, _, port = listen["addr"].rpartition(":")
    with (
        upstream.FakeUpstream(listen["proto"], addr, int(port)) as fake,
        unbound.UnboundProcess(conf) as proc,
    ):
        if not proc.wait_ready():
            report.note(f"unbound stderr:\n{proc.stderr}")
            report.check("unbound started", True, False)
            report.finish()
            return
        unbound.send_query(case.expect["query"], timeout=2.0)
        hit = fake.wait(timeout=6.0)
        report.note(f"fake upstream on {fake.where}: {hit or 'nothing arrived'}")
        if hit is None:
            report.note(f"unbound stderr:\n{proc.stderr}")
        report.check(f"traffic reached {fake.where}", True, hit is not None)


def _run_dig_case(case: Case, tmp: pathlib.Path, report: Report, opts) -> None:
    conf, _ = _checkconf_gate(case, tmp, report)
    proc = unbound.UnboundProcess(conf)
    if case.expect.get("starts") is False:
        # A config that checkconf passes but the daemon refuses: it must
        # exit on its own, and its log says why.
        with proc:
            code = proc.wait_exit(timeout=10.0)
        report.note(f"unbound exit code: {code}")
        report.check(
            "unbound exited on its own with a failure",
            True,
            code is not None and code != 0,
        )
        _check_stderr(case, report, proc.stderr)
        return
    with proc:
        if not proc.wait_ready():
            report.note(f"unbound stderr:\n{proc.stderr}")
            report.check("unbound started", True, False)
            report.finish()
            return
        answer = unbound.send_query(case.expect["query"])
        if answer is None:
            report.note(f"unbound stderr:\n{proc.stderr}")
            report.check("got a reply", True, False)
            report.finish()
            return
        _check_answer(case, report, answer)
        # The same question again, this time through dig, for the book to
        # include.  The checks above stay on the dnspython reply.
        _save_dig(case, opts)
    # Read the daemon log only after it has been stopped, so nothing is
    # still buffered in the pipe.
    _check_stderr(case, report, proc.stderr)


def _check_stderr(case: Case, report: Report, stderr: str) -> None:
    if "stderr_contains" in case.expect:
        report.check_contains("unbound stderr", case.expect["stderr_contains"], stderr)
    if "stderr_not_contains" in case.expect:
        report.check_not_contains(
            "unbound stderr", case.expect["stderr_not_contains"], stderr
        )


def _check_answer(case: Case, report: Report, answer) -> None:
    """The client side checks shared by run-and-dig and run-with-upstreams."""
    import dns.rcode

    report.note(f"reply: {answer.rcode()!r} answer section: {answer.answer!r}")
    if "rcode" in case.expect:
        report.check("rcode", case.expect["rcode"], dns.rcode.to_text(answer.rcode()))
    rendered = "\n".join(rr.to_text() for rr in answer.answer)
    for needle in case.expect.get("answer_contains", []):
        report.check_contains("answer section", needle, rendered)
    if "answer_count" in case.expect:
        count = sum(len(rrset) for rrset in answer.answer)
        report.check("records in answer section", case.expect["answer_count"], count)
    if "answer_ttl" in case.expect:
        ttls = sorted({rrset.ttl for rrset in answer.answer})
        report.check("answer TTL(s)", [case.expect["answer_ttl"]], ttls)
    authority = "\n".join(rr.to_text() for rr in answer.authority)
    for needle in case.expect.get("authority_contains", []):
        report.check_contains("authority section", needle, authority)


_DIG_ID = re.compile(r"id: \d+")


def _save_dig(case: Case, opts) -> None:
    """Write dig's output, unless only the message id would change.

    The id is random on every run, so rewriting it would churn every file
    and every excerpt the book has copied, while nothing else changed.  Any
    other difference (a TTL, a flag, a section) is written, so a changed
    answer still shows up in the diff.
    """
    if not opts.dig_out:
        return
    opts.dig_out.mkdir(parents=True, exist_ok=True)
    path = opts.dig_out / f"{case.id}.txt"
    text = unbound.dig_text(case.expect["query"])
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if _DIG_ID.sub("id: N", old) == _DIG_ID.sub("id: N", text):
            return
    path.write_text(text, encoding="utf-8")


#: How long run-with-upstreams waits for what an upstream must receive.
UPSTREAM_WAIT = 8.0

CLIENT_CHECKS = ("rcode", "answer_contains", "answer_count", "authority_contains")


def _run_upstreams_case(case: Case, tmp: pathlib.Path, report: Report, opts) -> None:
    import time

    conf, _ = _checkconf_gate(case, tmp, report)
    specs = case.expect["upstreams"]
    ups = []
    for spec in specs:
        addr, _, port = spec["addr"].rpartition(":")
        ups.append(
            upstream.ServingUpstream(spec["proto"], addr, int(port), spec["replies"])
        )
    with contextlib.ExitStack() as stack:
        for up in ups:
            stack.enter_context(up)
        proc = stack.enter_context(unbound.UnboundProcess(conf))
        if not proc.wait_ready():
            report.note(f"unbound stderr:\n{proc.stderr}")
            report.check("unbound started", True, False)
            report.finish()
            return
        answer = unbound.send_query(case.expect["query"], timeout=6.0)
        # Give what must arrive a chance to arrive, then look.
        deadline = time.monotonic() + UPSTREAM_WAIT
        while time.monotonic() < deadline:
            done = all(
                all(
                    need in {r.spec() for r in up.snapshot()}
                    for need in spec.get("received_contains", [])
                )
                and (not spec.get("tls_client_hello") or up.first_bytes)
                for spec, up in zip(specs, ups, strict=True)
            )
            if done:
                break
            time.sleep(0.1)
        if answer is not None and opts.dig_out:
            _save_dig(case, opts)
    for spec, up in zip(specs, ups, strict=True):
        got = up.snapshot()
        report.note(
            f"{up.where} received: {[(r.spec(), r.rd, r.proto) for r in got]} "
            f"first bytes: {[b.hex() for b in up.first_bytes]}"
        )
        specs_got = [r.spec() for r in got]
        for need in spec.get("received_contains", []):
            report.check(f"{up.where} received {need!r}", True, need in specs_got)
        if "received_first" in spec:
            report.check(
                f"{up.where} first query",
                spec["received_first"],
                specs_got[0] if specs_got else None,
            )
        if "received_rd" in spec:
            report.check(
                f"{up.where} RD bit of every query",
                {spec["received_rd"]},
                {r.rd for r in got} or None,
            )
        if spec.get("received_none"):
            report.check(f"{up.where} received nothing", [], specs_got + up.first_bytes)
        if spec.get("tls_client_hello"):
            report.check(
                f"{up.where} TCP stream starts with a TLS handshake record",
                True,
                bool(up.first_bytes) and all(b[:1] == b"\x16" for b in up.first_bytes),
            )
    if any(k in case.expect for k in CLIENT_CHECKS):
        if answer is None:
            report.check("got a reply", True, False)
        else:
            _check_answer(case, report, answer)
    _check_stderr(case, report, proc.stderr)


def _run_resolve_case(case: Case, tmp: pathlib.Path, report: Report, opts) -> None:
    conf, _ = _checkconf_gate(case, tmp, report, port=unbound.STUB_PORT)
    proc = unbound.UnboundProcess(conf, port=unbound.STUB_PORT)
    with proc, unbound.stub_resolver_pointed_at_unbound():
        if not proc.wait_ready():
            report.note(f"unbound stderr:\n{proc.stderr}")
            report.check("unbound started", True, False)
            report.finish()
            return
        result = unbound.getent_ahosts(case.expect["lookup"])
        # A name that must resolve through the same Unbound, so that a
        # failed lookup above cannot be the harness failing to reach it.
        control = (
            unbound.getent_ahosts(case.expect["control_lookup"])
            if "control_lookup" in case.expect
            else None
        )
    report.note(f"getent ahosts exit={result.exit_code} output={result.stdout!r}")
    if control is not None:
        report.note(f"control getent ahosts exit={control.exit_code}")
        report.check("control getent ahosts exit code", 0, control.exit_code)
        for needle in case.expect.get("control_contains", []):
            report.check_contains(
                "control getent ahosts output", needle, control.stdout
            )
    if case.expect.get("lookup_empty"):
        report.check("getent ahosts output", "", result.stdout)
    if "lookup_exit" in case.expect:
        report.check(
            "getent ahosts exit code", case.expect["lookup_exit"], result.exit_code
        )
    for needle in case.expect.get("lookup_contains", []):
        report.check_contains("getent ahosts output", needle, result.stdout)


RUNNERS = {
    "run-with-upstreams": _run_upstreams_case,
    "unbound-checkconf": _run_checkconf_case,
    "run-and-observe": _run_observe_case,
    "run-and-dig": _run_dig_case,
    "run-and-resolve": _run_resolve_case,
}


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case(case: Case, request) -> None:
    reason = _skip_reason(case, request.config.getoption("--run-unverified"))
    if reason:
        pytest.skip(reason)
    report = Report(case)
    with tempfile.TemporaryDirectory(prefix=f"ubt-{case.id}-") as td:
        RUNNERS[case.command](case, pathlib.Path(td), report, Options(request.config))
    report.finish()
