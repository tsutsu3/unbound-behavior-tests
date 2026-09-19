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

import pathlib
import tempfile

import pytest

from runner import unbound, upstream
from runner.cases import Case, load_cases

CASES = load_cases()


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


def _run_checkconf_case(case: Case, tmp: pathlib.Path, report: Report) -> None:
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


def _checkconf_gate(case: Case, tmp: pathlib.Path, report: Report):
    """Every runtime case also states what checkconf did with the config."""
    conf = unbound.write_config(tmp, case.config, case.files)
    result = unbound.run_checkconf(conf)
    report.note(f"unbound-checkconf exit={result.exit_code}")
    if "checkconf_exit" in case.expect:
        report.check(
            "unbound-checkconf exit code",
            case.expect["checkconf_exit"],
            result.exit_code,
        )
    return conf, result


def _run_observe_case(case: Case, tmp: pathlib.Path, report: Report) -> None:
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


def _run_dig_case(case: Case, tmp: pathlib.Path, report: Report) -> None:
    conf, _ = _checkconf_gate(case, tmp, report)
    proc = unbound.UnboundProcess(conf)
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
        import dns.rcode

        report.note(f"reply: {answer.rcode()!r} answer section: {answer.answer!r}")
        if "rcode" in case.expect:
            report.check(
                "rcode",
                case.expect["rcode"],
                dns.rcode.to_text(answer.rcode()),
            )
        rendered = "\n".join(rr.to_text() for rr in answer.answer)
        for needle in case.expect.get("answer_contains", []):
            report.check_contains("answer section", needle, rendered)
        if "answer_count" in case.expect:
            count = sum(len(rrset) for rrset in answer.answer)
            report.check(
                "records in answer section", case.expect["answer_count"], count
            )
        if "answer_ttl" in case.expect:
            ttls = sorted({rrset.ttl for rrset in answer.answer})
            report.check("answer TTL(s)", [case.expect["answer_ttl"]], ttls)
    # Read the daemon log only after it has been stopped, so nothing is
    # still buffered in the pipe.
    if "stderr_contains" in case.expect:
        report.check_contains(
            "unbound stderr", case.expect["stderr_contains"], proc.stderr
        )


RUNNERS = {
    "unbound-checkconf": _run_checkconf_case,
    "run-and-observe": _run_observe_case,
    "run-and-dig": _run_dig_case,
}


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case(case: Case, request) -> None:
    reason = _skip_reason(case, request.config.getoption("--run-unverified"))
    if reason:
        pytest.skip(reason)
    report = Report(case)
    with tempfile.TemporaryDirectory(prefix=f"ubt-{case.id}-") as td:
        RUNNERS[case.command](case, pathlib.Path(td), report)
    report.finish()
