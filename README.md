# unbound-behavior-tests

English | [日本語](README_ja.md)

An executable collection of cases that records how Unbound's configuration
behaves, both as **facts established by reading the source** and as
**results confirmed by actually running it**.

Target: Unbound **1.26.0** (tag `release-1.26.0`, commit `a45da353d3feb5d8fc00685fa1ceda3816d5108f`)

Scope: config file parsing, `local-zone` / `local-data`, `forward-zone` / `stub-zone`.

## Usage

Use Docker Compose. The working tree is mounted at `/work`, so editing
`cases/*.yaml` or the runner does not need an image rebuild.

```sh
docker compose build                     # first time, and after changing Dockerfile / uv.lock
docker compose run --rm tests            # run every case
```

Arguments given to `tests` are passed straight to pytest.

```sh
docker compose run --rm tests -k forward-addr-port-overflow   # a single case
docker compose run --rm tests --run-unverified                # also judge needs-test cases
docker compose run --rm tests -vv --color=yes                 # more detail
```

Generate the Appendix tables. The output goes to `out/` and is committed to
this repository. The English tables are `*.md` and the Japanese ones are
`*_ja.md`; both are generated from the same case files.

```sh
docker compose run --rm gen
```

Linting and formatting use Ruff.

```sh
docker compose run --rm lint                    # ruff check .
docker compose run --rm lint format --check .
docker compose run --rm lint check --fix .      # auto-fix
docker compose run --rm lint format .           # format
```

To poke around inside the container. `unbound` / `unbound-checkconf` / `dig` /
`pytest` / `ruff` are installed.

```sh
docker compose run --rm shell
```

`gen` and `lint` write files on the host, so they run as the host user.
If your uid/gid is not 1000, pass them to override.

```sh
UID=$(id -u) GID=$(id -g) docker compose run --rm gen
```

Without Compose:

```sh
docker build -t unbound-behavior-tests:1.26.0 .
docker run --rm -v "$PWD:/work" -w /work unbound-behavior-tests:1.26.0 pytest -q
```

If uv is installed on the host, `gen.py` and Ruff run without the container,
since they do not need Unbound. Running the cases needs Unbound 1.26.0, so use
the container for that.

```sh
uv run python gen.py
uv run ruff check .
uv run ruff format .
```

## Writing a case

One case = one file = one test = one row of the Appendix table.
No test code is written per case. `test_behavior.py` has a single function
that walks `cases/*.yaml` and feeds them to `pytest.mark.parametrize`.

```yaml
id: forward-addr-port-overflow          # must match the file name
title:
  en: "The forward-addr @port is not range-checked and is truncated to uint16"
  ja: forward-addr の @ポートは範囲検証されず uint16 に切り詰められる
chapter: "6.4"                          # section number in the book; TBD if undecided
category: A                             # A / B / C / D / undecided
versions: ["1.26.0"]
source:
  en: "util/net_help.c authextstrtoaddr() does port = atoi(s+1) without a range check"
  ja: "util/net_help.c authextstrtoaddr()。atoi() に範囲検証がない"
manual:                                 # where the man page says it; start with none if nowhere
  en: "none"
  ja: "none"
layers:                                 # a single layer is not made into a column
  en: ["address parsing"]
  ja: ["アドレス分解"]
status: verified                        # verified / needs-test / inconclusive
config: |
  forward-zone:
    name: "."
    forward-addr: 127.0.0.1@70000
command: run-and-observe
expect:
  checkconf_exit: 0
  listen:
    proto: udp
    addr: "127.0.0.1:4464"
  query: "example.com. A"
  note:
    en: "70000 is truncated to uint16 and becomes 4464"
    ja: "70000 は uint16 に切り詰められ 4464 になる"
```

`config` is **appended after** the fixed part (the `server:` clause) in
`runner/unbound.py`, so you can write `server:` options directly or open a
top-level clause such as `forward-zone:`.

### English and Japanese

The prose fields `title` / `source` / `manual` / `layers` / `expect.note` are
mappings with the two keys `en` and `ja`, **in that order**. Neither can be
left out.

- `layers` has the same number of items in both languages. Items at the same
  position name the same layer.
- Quotations from the man page stay in the original English in both.
  Do not translate them.
- The values of `config` / `files` / `expect` (except `note`) do not depend on
  the language, so they are written once.

### Cases with several files

A case that exercises `include:` / `include-toplevel:` puts a "relative path →
content" map in the optional `files:`. The files are written into the same
directory as the generated `unbound.conf`, and `unbound` / `unbound-checkconf`
start with that directory as their working directory, so relative paths work
as written.

```yaml
config: |
  include: "conf.d/*.conf"
files:
  conf.d/10-first.conf: |
    server:
        local-zone: "example.com." static
  conf.d/20-second.conf: |
    server:
        local-zone: "example.com." refuse
```

Paths stay inside the temporary directory. Absolute paths and paths containing
`..` are rejected at load time, and the name `unbound.conf` cannot be used
either (it would clash with the file generated from `config:`).

### command and expect

| command             | What it does                                         | Keys allowed in expect                                                                                         |
| ------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `unbound-checkconf` | Writes the config and runs `unbound-checkconf`       | `exit`, `stderr_contains`, `note`                                                                              |
| `run-and-observe`   | Starts the daemon and checks what reaches a fake upstream | `checkconf_exit`, `listen{proto,addr}`, `query`, `note`                                                   |
| `run-and-dig`       | Starts the daemon and checks the content of the answer | `checkconf_exit`, `query`, `rcode`, `answer_contains`, `answer_count`, `answer_ttl`, `stderr_contains`, `note` |

`stderr_contains` in `run-and-dig` is read after the daemon has stopped.
Writing `verbosity: 4` in the config lets the detailed internal log be used in
the check (`unbound-checkconf` does not set the global `verbosity`, so this is
visible only from the daemon).

### status and pytest

| status         | What happens                                                               |
| -------------- | -------------------------------------------------------------------------- |
| `verified`     | Run and compared. Any mismatch fails                                       |
| `needs-test`   | Skipped. Judged only with `--run-unverified`                               |
| `inconclusive` | Always skipped. The case records that something is still open, not an answer |

### Do not widen the schema on your own

`runner/cases.py` checks every case at load time.

- Unknown keys are errors. The keys allowed in `expect` are fixed per `command`.
  The only optional top-level key is `files`.
- `id` must match the file name.
- Prose fields need both `en` and `ja`. `layers` must have the same number of
  items in both.
- If `category` is `B` or `C`, a `manual.en` starting with `none` is not allowed
  (B and C are both **claims about the man page**, so they cannot be made
  without quoting it).
- An unquoted value containing ` #` is an error, because YAML would eat it as
  a comment and the Appendix row would stop mid-sentence.

If a case cannot be expressed, report the proposed extension and the reason
first, then change the schema.

## Keep the kinds of evidence apart

- **Source** — confirmed by reading the source. Record the file and function.
- **Test** — confirmed by actually running 1.26.0.
- **Manual** — the man page says so. **Quote the original text.**
- **Unconfirmed** — none of the above.

Do not present a Source-only claim as tested, or the other way round.
Write `expect` **from the source first, as a prediction**. Do not fill it in
from the results.

## Layout

| Path                 | Contents                                           |
| -------------------- | -------------------------------------------------- |
| `cases/`             | Case definitions                                   |
| `runner/cases.py`    | YAML loading and schema validation                 |
| `runner/unbound.py`  | Config generation, starting Unbound, sending queries |
| `runner/upstream.py` | Fake upstream server (observes the destination)    |
| `test_behavior.py`   | pytest entry point (a single function)             |
| `gen.py`             | Generates the Appendix tables and summary (English and Japanese) |
| `out/`               | Output of `gen.py`. Committed                      |
| `README_ja.md`       | Japanese version of this README                    |
| `references/`        | Unbound checkout. **Not tracked by Git**           |

Preparing `references/`:

```sh
git clone --depth 1 --branch release-1.26.0 \
  https://github.com/NLnetLabs/unbound.git references/unbound-1.26.0
```

It is not needed to run the tests (the Docker image builds from the release
tarball). It is useful for following the line numbers cited in the cases.
