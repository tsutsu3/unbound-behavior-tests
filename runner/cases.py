"""Load and validate the behaviour cases in cases/*.yaml.

One case is one test and one row of the generated Appendix table, so the
schema is deliberately small and validated strictly: an unknown key is an
error rather than a silently ignored field.  Extending the schema is a
decision to be reported first (see README), not something a new case file
should be able to do on its own.
"""

import dataclasses
import pathlib

import yaml

CASES_DIR = pathlib.Path(__file__).resolve().parent.parent / "cases"

#: Statuses a case may carry.  See README for what each means for pytest.
STATUSES = ("verified", "needs-test", "inconclusive")

#: Classification of the claim *about the manual*.  See README.
CATEGORIES = ("A", "B", "C", "D", "undecided")

#: Every prose field is written in each of these languages.  English comes
#: first because it is the repository's primary language; Japanese is kept
#: alongside so the Japanese tables are generated from the same case files.
LANGS = ("en", "ja")

COMMANDS = ("unbound-checkconf", "run-and-observe", "run-and-dig")

TOP_LEVEL_REQUIRED = (
    "id",
    "title",
    "chapter",
    "category",
    "versions",
    "source",
    "manual",
    "layers",
    "status",
    "config",
    "command",
    "expect",
)
#: `files` is optional: extra files written next to unbound.conf so a case
#: can exercise `include:` / `include-toplevel:`.  Keys are paths relative to
#: the generated config's directory, which is also the working directory the
#: binaries run in, so a case writes `include: "common.conf"` and nothing
#: has to know the temporary directory's name.
TOP_LEVEL_OPTIONAL = ("files",)

EXPECT_KEYS = {
    "unbound-checkconf": {"exit", "stderr_contains", "note"},
    "run-and-observe": {"checkconf_exit", "listen", "query", "note"},
    "run-and-dig": {
        "checkconf_exit",
        "query",
        "rcode",
        "answer_contains",
        "answer_ttl",
        "answer_count",
        "stderr_contains",
        "note",
    },
}

LISTEN_KEYS = {"proto", "addr"}


class CaseError(ValueError):
    """A case file does not satisfy the schema."""


@dataclasses.dataclass(frozen=True)
class Text:
    """A prose field, written once per language in LANGS."""

    en: str
    ja: str

    def __getitem__(self, lang: str) -> str:
        return getattr(self, lang)


@dataclasses.dataclass(frozen=True)
class Layers:
    """The named layers, per language.  Both lists have the same length."""

    en: tuple[str, ...]
    ja: tuple[str, ...]

    def __getitem__(self, lang: str) -> tuple[str, ...]:
        return getattr(self, lang)

    def __len__(self) -> int:
        return len(self.en)


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    title: Text
    chapter: str
    category: str
    versions: tuple[str, ...]
    source: Text
    manual: Text
    layers: Layers
    status: str
    config: str
    command: str
    expect: dict
    path: pathlib.Path
    files: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def is_column_candidate(self) -> bool:
        """A behaviour decided by a single layer is explained in one line."""
        return len(self.layers) >= 2

    @property
    def runs(self) -> bool:
        return self.status == "verified"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise CaseError(msg)


def _parse_files(path: pathlib.Path, raw: dict) -> dict[str, str]:
    """Validate the optional `files` mapping.

    Paths stay inside the case's temporary directory: no absolute paths and
    no `..`, so a case cannot write outside it or read the host's config.
    """
    files = raw.get("files", {})
    if files is None:
        files = {}
    _require(isinstance(files, dict), f"{path.name}: files must be a mapping")
    for name, content in files.items():
        _require(
            isinstance(name, str) and name != "",
            f"{path.name}: files keys must be non-empty strings",
        )
        _require(
            isinstance(content, str),
            f"{path.name}: files[{name!r}] must be a string",
        )
        rel = pathlib.PurePosixPath(name)
        _require(
            not rel.is_absolute() and ".." not in rel.parts,
            f"{path.name}: files[{name!r}] must be a relative path without '..'",
        )
        _require(
            name != "unbound.conf",
            f"{path.name}: files may not define 'unbound.conf'; that name is "
            f"the generated config built from `config:`",
        )
    return dict(files)


def _parse_text(path: pathlib.Path, where: str, value) -> Text:
    """Validate a prose field: a mapping with exactly one string per language."""
    _require(
        isinstance(value, dict) and list(value) == list(LANGS),
        f"{path.name}: {where} must be a mapping with the keys {list(LANGS)} "
        f"in that order",
    )
    for lang in LANGS:
        _require(
            isinstance(value[lang], str) and value[lang].strip(),
            f"{path.name}: {where}.{lang} must be a non-empty string",
        )
    return Text(**value)


def _parse_layers(path: pathlib.Path, value) -> Layers:
    """Validate `layers`: one list per language, index i naming the same layer."""
    _require(
        isinstance(value, dict) and list(value) == list(LANGS),
        f"{path.name}: layers must be a mapping with the keys {list(LANGS)} "
        f"in that order",
    )
    for lang in LANGS:
        items = value[lang]
        _require(
            isinstance(items, list)
            and items
            and all(isinstance(x, str) and x.strip() for x in items),
            f"{path.name}: layers.{lang} must be a non-empty list of strings",
        )
    lengths = {lang: len(value[lang]) for lang in LANGS}
    _require(
        len(set(lengths.values())) == 1,
        f"{path.name}: layers must name the same number of layers in every "
        f"language, got {lengths}",
    )
    return Layers(**{lang: tuple(value[lang]) for lang in LANGS})


def _lint_unquoted_hash(path: pathlib.Path, text: str) -> None:
    """Catch a `#` that YAML would silently eat as a comment.

    Several of the behaviours recorded here are about `#`, so an unquoted
    scalar containing one truncates the field without any error.  Refuse it
    rather than generate an Appendix row that stops mid-sentence.
    """
    for lineno, line in enumerate(text.splitlines(), 1):
        key, sep, value = line.partition(": ")
        if not sep or not key.strip() or key.lstrip().startswith("#"):
            continue
        value = value.strip()
        if value[:1] in ('"', "'", "|", ">", "["):
            continue
        if " #" in value:
            raise CaseError(
                f"{path.name}:{lineno}: unquoted value contains ' #', which "
                f"YAML would treat as a comment -- quote it: {line.strip()!r}"
            )


def parse_case(path: pathlib.Path) -> Case:
    text = path.read_text(encoding="utf-8")
    _lint_unquoted_hash(path, text)
    raw = yaml.safe_load(text)
    _require(isinstance(raw, dict), f"{path.name}: top level must be a mapping")

    keys = set(raw)
    missing = set(TOP_LEVEL_REQUIRED) - keys
    _require(not missing, f"{path.name}: missing keys {sorted(missing)}")
    unknown = keys - set(TOP_LEVEL_REQUIRED) - set(TOP_LEVEL_OPTIONAL)
    _require(not unknown, f"{path.name}: unknown keys {sorted(unknown)}")

    case_id = raw["id"]
    _require(
        case_id == path.stem,
        f"{path.name}: id {case_id!r} must match the file name",
    )
    _require(
        raw["status"] in STATUSES,
        f"{path.name}: status must be one of {STATUSES}",
    )
    _require(
        raw["category"] in CATEGORIES,
        f"{path.name}: category must be one of {CATEGORIES}",
    )
    _require(
        raw["command"] in COMMANDS,
        f"{path.name}: command must be one of {COMMANDS}",
    )
    _require(
        isinstance(raw["versions"], list) and raw["versions"],
        f"{path.name}: versions must be a non-empty list",
    )
    _require(
        isinstance(raw["chapter"], str) and raw["chapter"],
        f"{path.name}: chapter must be a string (use 'TBD' if undecided)",
    )
    title = _parse_text(path, "title", raw["title"])
    source = _parse_text(path, "source", raw["source"])
    manual = _parse_text(path, "manual", raw["manual"])
    layers = _parse_layers(path, raw["layers"])
    # A and D are claims about code or about versions; B and C are claims
    # about the manual, so they cannot be made without citing it.
    _require(
        raw["category"] not in ("B", "C") or not manual.en.startswith("none"),
        f"{path.name}: category {raw['category']} requires a manual reference",
    )

    expect = raw["expect"]
    _require(isinstance(expect, dict), f"{path.name}: expect must be a mapping")
    if "note" in expect:
        expect["note"] = _parse_text(path, "expect.note", expect["note"])
    allowed = EXPECT_KEYS[raw["command"]]
    unknown = set(expect) - allowed
    _require(
        not unknown,
        f"{path.name}: expect keys {sorted(unknown)} not valid for "
        f"command {raw['command']} (allowed: {sorted(allowed)})",
    )
    if raw["command"] == "run-and-observe":
        listen = expect.get("listen")
        _require(isinstance(listen, dict), f"{path.name}: expect.listen required")
        _require(
            set(listen) == LISTEN_KEYS,
            f"{path.name}: expect.listen keys must be {sorted(LISTEN_KEYS)}",
        )
        _require(
            listen["proto"] in ("udp", "tcp"),
            f"{path.name}: expect.listen.proto must be udp or tcp",
        )

    return Case(
        id=case_id,
        title=title,
        chapter=raw["chapter"],
        category=raw["category"],
        versions=tuple(raw["versions"]),
        source=source,
        manual=manual,
        layers=layers,
        status=raw["status"],
        config=raw["config"],
        command=raw["command"],
        expect=expect,
        path=path,
        files=_parse_files(path, raw),
    )


def load_cases(directory: pathlib.Path | None = None) -> list[Case]:
    directory = directory or CASES_DIR
    cases = [parse_case(p) for p in sorted(directory.glob("*.yaml"))]
    seen: dict[str, pathlib.Path] = {}
    for case in cases:
        if case.id in seen:
            raise CaseError(f"duplicate case id {case.id!r}")
        seen[case.id] = case.path
    return cases
