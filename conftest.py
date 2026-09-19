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
