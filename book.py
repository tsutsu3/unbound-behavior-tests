"""Render the book's Appendix pages on undocumented behaviour (Japanese).

`gen.py --book <docs> --book-out <dir>` calls this.  Unlike the tables in
out/, these pages depend on the book: each item links to the body page that
cites its case, so the book's pages are an input.  That is why the output is
written straight into the directory given on the command line and not into
out/, where CI checks that nothing is stale without the book at hand.

What goes where:

* `undocumented-index.md` -- one table of every item, C first.
* `undocumented-<chapter>-<page>.md` -- the items whose case is first cited
  by that body page, with the configuration, the result and the evidence.
* `undocumented-other.md` -- items cited only from the cookbook, or not at all.

The body page of an item is found by searching the book for the case id in
backquotes, which is how the book's footnotes cite a case.  The `chapter`
field of a case is not used for this: it predates the book's page layout.

Cases in category `undecided` are left out, and so is D, which has its own
page.  Every page is marked `access: private`; the book decides nothing else
about publication.
"""

import json
import pathlib
import re

from runner.cases import Case

VERSION = "1.26.0"
TAG = "release-1.26.0"
SOURCE_URL = f"https://github.com/NLnetLabs/unbound/blob/{TAG}"

#: Category definitions, word for word as the book defines them.  They are
#: about the man page, so they go into a footnote: the book does not make the
#: man page the subject of body text.
CATEGORIES = {
    "C": "man に記述はあるが結果が直感に反する",
    "B": "man の記述と実装が食い違う / 曖昧",
    "A": "man に記述がない挙動",
}
#: What headings and tables call each category.
LABELS = {
    "C": "説明どおりだが直感に反する挙動",
    "B": "説明と実装が食い違う挙動",
    "A": "説明のない挙動",
}
#: C first: it is the trap that bites even readers of the man page.
ORDER = ("C", "B", "A")

#: Directories whose pages are body pages in the book's reading order.
#: The cookbook is searched after them, so an item cited from both a chapter
#: and a recipe belongs to the chapter.
SKIP_DIRS = {"appendix", "dev", "09-cookbook"}
COOKBOOK = "09-cookbook"

#: Top level clauses of unbound.conf that may appear in a case config.
CLAUSES = {
    "server",
    "forward-zone",
    "stub-zone",
    "auth-zone",
    "view",
    "rpz",
    "remote-control",
    "python",
    "dynlib",
    "cachedb",
    "dnstap",
    "ipset",
}

PATH_RE = re.compile(
    r"(?<![`/\w])((?:[a-z0-9_-]+/)+[A-Za-z0-9_.-]+\.(?:c|h|lex|y|in|rst))(?![\w`])"
)
FUNC_RE = re.compile(r"(?<![`\w])([A-Za-z_][A-Za-z0-9_]*\(\))(?!`)")
TITLE_RE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)


class BookPage:
    """A body page of the book: where it is, what it is called, its text."""

    def __init__(self, docs: pathlib.Path, path: pathlib.Path):
        self.rel = path.relative_to(docs).with_suffix("")
        text = path.read_text(encoding="utf-8")
        m = TITLE_RE.search(text)
        self.title = m.group(1).strip() if m else str(self.rel)
        self.text = text

    @property
    def url(self) -> str:
        return f"/ja/{self.rel.as_posix()}/"

    @property
    def chapter_dir(self) -> str:
        return self.rel.parts[0]

    @property
    def is_cookbook(self) -> bool:
        return self.chapter_dir == COOKBOOK

    @property
    def slug(self) -> str:
        """`05-02` for `05-local-dns/02-zone-types`."""
        chapter = self.chapter_dir.split("-", 1)[0]
        page = self.rel.name.split("-", 1)[0]
        return f"{chapter}-{page}"

    def cites(self, case: Case) -> bool:
        return f"`{case.id}`" in self.text


def load_pages(docs: pathlib.Path) -> list[BookPage]:
    """Body pages in reading order, the cookbook last."""
    chapters = sorted(
        p
        for p in docs.glob("*/*.md")
        if p.parent.name not in SKIP_DIRS and p.parent.name[:2].isdigit()
    )
    if not chapters:
        raise SystemExit(
            f"no body pages under {docs}; pass the book's src/content/docs/ja"
        )
    recipes = sorted((docs / COOKBOOK).glob("*.md"))
    return [BookPage(docs, p) for p in chapters + recipes]


def home_page(case: Case, pages: list[BookPage]) -> BookPage | None:
    return next((p for p in pages if p.cites(case)), None)


# --- text helpers --------------------------------------------------------


def code(text: str) -> str:
    """Inline code that survives backquotes in the text."""
    fence = "``" if "`" in text else "`"
    pad = " " if fence == "``" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def md(text: str) -> str:
    """Prose from a case, escaped so Markdown shows it as written.

    Code spans the case already writes in backquotes are kept as they are.
    """
    parts = re.split(r"(`[^`]*`)", text)
    return "".join(
        part if i % 2 else re.sub(r"([\\*<>\[\]])", r"\\\1", part)
        for i, part in enumerate(parts)
    )


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def link_source(text: str) -> str:
    """Link each source file once and put function names in backquotes."""
    seen: set[str] = set()

    def path(m: re.Match) -> str:
        p = m.group(1)
        if p in seen:
            return code(p)
        seen.add(p)
        return f"[{code(p)}]({SOURCE_URL}/{p})"

    return FUNC_RE.sub(lambda m: code(m.group(1)), PATH_RE.sub(path, text))


def manual_text(text: str) -> str:
    """`none (...)` in a case means the man page says nothing."""
    m = re.match(r"none\s*[（(](.*)[）)]\s*$", text, re.DOTALL)
    if m:
        return f"記述なし（{m.group(1).strip()}）"
    if text.strip() == "none":
        return "記述なし"
    return text


def sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith(("。", "）", ")", ".")) else text + "。"


def display_config(config: str) -> str:
    """The case config as a reader would write it.

    The harness appends the case text to a `server:` clause of its own, so
    options before the first clause belong to `server:`.  Indentation means
    nothing to Unbound; it is normalised to four spaces under each clause.
    """
    out: list[str] = []
    opened = False
    for raw in config.rstrip("\n").splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        name = line.split(":", 1)[0]
        if not raw.startswith((" ", "\t")) and name in CLAUSES and line == f"{name}:":
            out.append(line)
            opened = True
            continue
        if not opened:
            out.append("server:")
            opened = True
        out.append(f"    {line}")
    return "\n".join(out)


# --- result table --------------------------------------------------------


def result_rows(case: Case) -> list[tuple[str, str]]:
    e = case.expect
    rows: list[tuple[str, str]] = []

    def listed(values: list[str]) -> str:
        return "、".join(code(v) for v in values)

    if case.command == "unbound-checkconf":
        rows.append(("`unbound-checkconf` の終了コード", str(e["exit"])))
    elif "checkconf_exit" in e:
        rows.append(("`unbound-checkconf` の終了コード", str(e["checkconf_exit"])))
    if "starts" in e:
        rows.append(("Unbound の起動", "成功" if e["starts"] else "失敗"))
    if "listen" in e:
        listen = e["listen"]
        rows.append(
            (
                "Unbound が問い合わせを送った先",
                code(f"{listen['addr']} ({listen['proto']})"),
            )
        )
    for up in e.get("upstreams", []):
        addr = code(up["addr"])
        answers = [a for r in up.get("replies", []) for a in r.get("answer", [])]
        if answers:
            rows.append((f"上流 {addr} が返す答え", listed(answers)))
        if up.get("received_none"):
            rows.append((f"上流 {addr} に届いた問い合わせ", "なし"))
        if "received_contains" in up:
            rows.append(
                (f"上流 {addr} に届いた問い合わせ", listed(up["received_contains"]))
            )
        if "received_first" in up:
            rows.append(
                (f"上流 {addr} に最初に届いた問い合わせ", code(up["received_first"]))
            )
        if "received_rd" in up:
            rows.append(
                (
                    f"上流 {addr} に届いた問い合わせの RD ビット",
                    "あり" if up["received_rd"] else "なし",
                )
            )
        if up.get("tls_client_hello"):
            rows.append((f"上流 {addr} に届いたもの", "TLS の ClientHello"))
    if "query" in e:
        rows.append(("問い合わせ", code(e["query"])))
    if "rcode" in e:
        rows.append(("応答コード", code(e["rcode"])))
    if "answer_contains" in e:
        rows.append(("ANSWER に含まれるもの", listed(e["answer_contains"])))
    if "answer_count" in e:
        rows.append(("ANSWER の RR の数", str(e["answer_count"])))
    if "answer_ttl" in e:
        rows.append(("ANSWER の TTL", str(e["answer_ttl"])))
    if "authority_contains" in e:
        rows.append(("AUTHORITY に含まれるもの", listed(e["authority_contains"])))
    if "lookup" in e:
        rows.append(("`getent ahosts` で引いた名前", code(e["lookup"])))
    if "lookup_exit" in e:
        rows.append(("`getent ahosts` の終了コード", str(e["lookup_exit"])))
    if e.get("lookup_empty"):
        rows.append(("`getent ahosts` の出力", "なし"))
    if "lookup_contains" in e:
        rows.append(
            ("`getent ahosts` の出力に含まれるもの", listed(e["lookup_contains"]))
        )
    if "control_lookup" in e:
        rows.append(("対照として引いた名前", code(e["control_lookup"])))
    if "control_contains" in e:
        rows.append(("対照の出力に含まれるもの", listed(e["control_contains"])))
    for key, label in (
        ("stderr_contains", "出力に含まれるもの"),
        ("stderr_not_contains", "出力に含まれないもの"),
    ):
        if key in e:
            values = e[key] if isinstance(e[key], list) else [e[key]]
            rows.append((label, listed(values)))
    return rows


# --- pages ---------------------------------------------------------------


def frontmatter(title: str, description: str) -> str:
    return (
        "---\n"
        "# generated by gen.py --book. DO NOT EDIT.\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "access: private\n"
        "---\n"
    )


def item(case: Case, page: BookPage | None, home: BookPage | None) -> str:
    out = [f"### {md(case.title['ja'])}\n"]
    out.append(f"次の設定で確かめました[^{case.id}]。\n")
    out.append(f"```conf\n{display_config(case.config)}\n```\n")
    for name, content in case.files.items():
        out.append(f'```conf title="{name}"\n{content.rstrip()}\n```\n')
    rows = result_rows(case)
    if rows:
        out.append("| 確かめたこと | 結果 |\n| --- | --- |")
        out.extend(f"| {cell(k)} | {cell(v)} |" for k, v in rows)
        out.append("")
    # The page's own topic is linked once at the top; only other pages here.
    if page is not None and (home is None or page.rel != home.rel):
        out.append(f"関連する本文: [{page.title}]({page.url})\n")
    return "\n".join(out)


def footnote(case: Case) -> str:
    parts = [f"ケース {code(case.id)}。Unbound {VERSION} で実行。"]
    note = case.expect.get("note")
    if note:
        parts.append(sentence(md(note["ja"])))
    parts.append(f"実装: {sentence(link_source(md(case.source['ja'])))}")
    parts.append(f"man: {sentence(link_source(md(manual_text(case.manual['ja']))))}")
    return f"[^{case.id}]: " + "".join(parts)


def detail_page(
    title: str,
    description: str,
    intro: str,
    home: BookPage | None,
    items: list[tuple[Case, BookPage | None]],
) -> str:
    out = [frontmatter(title, description), intro]
    for cat in ORDER:
        group = [(c, p) for c, p in items if c.category == cat]
        if not group:
            continue
        out.append(f"## 分類 {cat}: {LABELS[cat]}\n")
        out.extend(item(c, p, home) for c, p in group)
    out.append("\n".join(footnote(c) for c, _ in items) + "\n")
    return "\n".join(out)


def detail_intro(scope: str) -> str:
    return (
        f"{scope}\n\n"
        f"どの項目も Unbound {VERSION} で確かめた結果です。設定には、"
        "待ち受けのアドレスやアクセス制御など、実行に要る定型の行を省いています。"
        "分類の意味は[未文書化の挙動](/ja/appendix/undocumented-index/)にあります。\n"
    )


def render(cases: list[Case], docs: pathlib.Path) -> dict[str, str]:
    pages = load_pages(docs)
    items = [c for c in cases if c.category in ORDER]
    items.sort(key=lambda c: (ORDER.index(c.category), c.id))

    grouped: dict[str, tuple[BookPage | None, list[tuple[Case, BookPage | None]]]] = {}
    for c in items:
        page = home_page(c, pages)
        key = page.slug if page is not None and not page.is_cookbook else "other"
        grouped.setdefault(key, (page if key != "other" else None, []))[1].append(
            (c, page)
        )

    files: dict[str, str] = {}
    index_rows: list[tuple[Case, str, str]] = []
    for key in sorted(grouped, key=lambda k: (k == "other", k)):
        home, group = grouped[key]
        name = f"undocumented-{key}"
        if home is not None:
            title = f"未文書化の挙動: {home.title}"
            description = (
                f"本文「{home.title}」が扱う範囲で、公式の説明に書かれていない、"
                f"または書かれていても意外な Unbound {VERSION} の挙動を、設定と結果と根拠で示します。"
            )
            scope = f"[{home.title}]({home.url})が扱う範囲の挙動を、分類ごとに並べています。"
        else:
            title = "未文書化の挙動: その他"
            description = f"本文の章では扱っていない Unbound {VERSION} の挙動を、設定と結果と根拠で示します。"
            scope = "本文の章では扱っていない挙動を、分類ごとに並べています。Cookbook のレシピで触れているものは、そのレシピへのリンクを付けています。"
        files[f"{name}.md"] = detail_page(
            title, description, detail_intro(scope), home, group
        )
        for c, page in group:
            body = f"[{page.title}]({page.url})" if page is not None else "—"
            index_rows.append((c, body, f"/ja/appendix/{name}/"))

    files["undocumented-index.md"] = index_page(index_rows)
    return files


def index_page(rows: list[tuple[Case, str, str]]) -> str:
    counts = {cat: sum(1 for c, _, _ in rows if c.category == cat) for cat in ORDER}
    out = [
        frontmatter(
            "未文書化の挙動",
            f"公式の説明に書かれていない、または書かれていても意外な Unbound {VERSION} の挙動を、分類ごとに一覧にした表です。",
        ),
        (
            f"Unbound {VERSION} で確かめた挙動のうち、公式の説明だけでは予想できないものを一覧にしています[^categories]。"
            "挙動の欄のリンク先に、各項目の設定、結果、根拠があります。\n"
        ),
        "| 分類 | 内容 | 件数 |\n| --- | --- | --- |",
    ]
    out.extend(f"| {cat} | {LABELS[cat]} | {counts[cat]} |" for cat in ORDER)
    out.append("\n分類 C は、説明を読んでいても踏む挙動なので、先頭に置いています。\n")
    for cat in ORDER:
        group = [r for r in rows if r[0].category == cat]
        if not group:
            continue
        out.append(f"## 分類 {cat}: {LABELS[cat]}\n")
        out.append("| 挙動 | 本文 |\n| --- | --- |")
        out.extend(
            f"| [{cell(md(c.title['ja']))}]({detail}) | {body} |"
            for c, body, detail in group
        )
        out.append("")
    definitions = "".join(f"{cat}: {CATEGORIES[cat]}。" for cat in reversed(ORDER))
    out.append(
        f"[^categories]: 分類の定義。{definitions}"
        "「説明」は `unbound.conf(5)` をはじめとする man ページを指す。"
        "バージョンによる違い（分類 D）は、この一覧には含めない。\n"
    )
    return "\n".join(out)
