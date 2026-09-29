"""pytest wiring for the behaviour cases.

Only the option plumbing lives here; the case walk itself is in
test_behavior.py so that there is exactly one test function for all cases.
"""


def pytest_addoption(parser):
    parser.addoption(
        "--run-unverified",
        action="store_true",
        default=False,
        help=(
            "also run and judge cases with status: needs-test. Use this while "
            "pinning down an expected value; do not use it to decide whether "
            "the suite passes."
        ),
    )
    parser.addoption(
        "--dig-out",
        default=None,
        metavar="DIR",
        help=(
            "also run dig for every run-and-dig case (and run-with-upstreams "
            "case on unprivileged ports) and write its output to DIR/<id>.txt, "
            "for the book to include. Only those cases are collected, so this "
            "runs as a normal user (see the dig service in compose.yaml)."
        ),
    )


def pytest_configure(config):
    """With --dig-out, record which dig produced the files next to them."""
    dig_out = config.getoption("--dig-out")
    if not dig_out:
        return
    import pathlib
    import subprocess

    out = pathlib.Path(dig_out)
    out.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["dig", "-v"], capture_output=True, text=True, check=False)
    (out / "_dig-version.txt").write_text(
        (p.stdout + p.stderr).strip() + "\n", encoding="utf-8"
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--dig-out"):
        return
    keep, drop = [], []
    for item in items:
        case = item.callspec.params.get("case") if hasattr(item, "callspec") else None
        (keep if case is not None and _digs(case) else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def _digs(case) -> bool:
    """Cases whose answer is worth saving, and that run as a normal user.

    run-with-upstreams cases qualify only when no fake upstream needs a
    privileged port, since the dig service runs as the host user.
    """
    if case.command == "run-and-dig":
        return True
    if case.command == "run-with-upstreams":
        return all(
            int(up["addr"].rpartition(":")[2]) >= 1024
            for up in case.expect["upstreams"]
        )
    return False
