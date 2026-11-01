import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EN = ROOT / "README.md"
JA = ROOT / "README.ja.md"

HEADING_RE = re.compile(r"^(#{1,6}) (.*)$")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
TOOL_CALL_RE = re.compile(r"`([a-z_]{3,}\([^`]*\))`")

# 起動してはいけないポート（このリポジトリは何も listen しないことを保証する）
FORBIDDEN_PORTS = {8080, 18080, 18081, 8102, 8103, 8795, 11434, 18789, 8384, 22000}
# サーバーとして listen する印のみ列挙（クライアント側 socket や AF_INET は許可）
LISTEN_RE = re.compile(
    r"socket\.bind\s*\(|\.listen\s*\(|serve_forever|\bHTTPServer\b|\bsocketserver\b"
    r"|socket\.create_server|\bhttp\.server\b|\buvicorn\b|\bflask\b|fastapi")

# 対応する EN/JA の見出しペア（同じ index に同じ意味の節が並ぶことを保証する）
SECTION_PAIRS = [
    ("Account Keeper Agent 🗝️", "Account Keeper Agent 🗝️"),
    ("What it can do (mock version)", "できること（モック版）"),
    ("Quick start", "クイックスタート"),
    ("Local LLM agent (Ollama)", "ローカル LLM エージェント（Ollama）"),
    ("Use from Hermes Agent / Claude Code (MCP)", "Hermes Agent / Claude Code から使う（MCP）"),
    ("Architecture", "アーキテクチャ"),
    ("Data model", "データモデル"),
    ("Roadmap", "ロードマップ"),
    ("Related products", "関連プロダクト"),
    ("License", "License"),
]

# Client-side markers that make a forbidden port mention safe (URL, comment, constant, env var, ...)
PORT_HINTS = ("://", "URL", "url", "PORT", "port", "#", "DEFAULT", "environ", "getenv",
              "connect", "client", "base_url", "API", "ollama", "Ollama",
              "localhost", "127.0.0.1", "0.0.0.0", "LLM", "llm", 'os.', "self.")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _outside_code(lines: list[str]) -> list[str]:
    out: list[str] = []
    fence = False
    for line in lines:
        if line.startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append(line)
    return out


def _fences(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def _headings(lines: list[str]) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2))
            for m in (HEADING_RE.match(l) for l in _outside_code(lines)) if m]


def _slug(heading: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.lower()))


def _links(lines: list[str]) -> list[str]:
    return [m.group(2) for m in (LINK_RE.search(l) for l in _outside_code(lines)) if m]


def _first_output_fence(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if line.startswith("```text"):
            return i
    raise AssertionError("no sample output block")  # kept for possible future tests


def test_both_readmes_exist_with_toggle_directly_under_title():
    for path, expected in ((EN, "English | [日本語](README.ja.md)"),
                           (JA, "[English](README.md) | 日本語")):
        assert path.exists(), path
        lines = _lines(path)
        title = next(i for i, l in enumerate(lines) if l.startswith("# "))
        window = lines[title + 1: title + 4]
        assert expected in window, f"toggle link missing in {path.name}"
        assert window.index(expected) == 1, f"toggle must sit directly under the title in {path.name}"


def test_heading_count_and_order_match():
    en, ja = _headings(_lines(EN)), _headings(_lines(JA))
    assert len(en) == len(ja), (len(en), len(ja))
    assert [h[0] for h in en] == [h[0] for h in ja], [(h[1], j[1]) for h, j in zip(en, ja)]
    assert [(e[1], j[1]) for e, j in zip(en, ja)] == SECTION_PAIRS, \
        [(e[1], j[1]) for e, j in zip(en, ja)]


def test_local_links_resolve_to_existing_files():
    for path in (EN, JA):
        for href in _links(_lines(path)):
            if href.startswith("#") or "://" in href:
                continue
            assert (ROOT / href).exists(), f"broken link {href!r} in {path.name}"


def test_example_config_links_exist_in_both_readmes():
    for href in ("examples/hermes-config.yaml", "examples/claude-code.mcp.json"):
        assert (ROOT / href).exists(), href
        for path in (EN, JA):
            assert href in _links(_lines(path)), f"{href} not linked from {path.name}"


def test_both_readmes_point_at_the_same_targets():
    def external(path):
        return {h for h in _links(_lines(path)) if "://" in h}

    def anchors(path):
        return {h for h in _links(_lines(path)) if h.startswith("#")}

    assert external(EN) == external(JA)
    assert anchors(EN) and anchors(JA)
    for path in (EN, JA):
        heads = _headings(_lines(path))
        for anchor in anchors(path):
            target = anchor[1:]
            indexes = [i for i, (_, title) in enumerate(heads) if _slug(title) == target]
            assert len(indexes) == 1, f"anchor {anchor} in {path.name} resolves to {indexes}"
    anchor_indexes = []
    for path in (EN, JA):
        heads = _headings(_lines(path))
        anchor_indexes.append(sorted(
            i for i, (_, title) in enumerate(heads) if _slug(title) in {a[1:] for a in anchors(path)}))
    assert anchor_indexes[0] == anchor_indexes[1]


def test_code_examples_are_identical_in_both_readmes():
    en = {m.group(1) for m in (TOOL_CALL_RE.search(l) for l in _lines(EN)) if m}
    ja = {m.group(1) for m in (TOOL_CALL_RE.search(l) for l in _lines(JA)) if m}
    assert en == ja, en ^ ja
    calls = [c for c in en if c.startswith("plan_change(")]
    assert calls == ['plan_change(field="address_label", old_value="旧住所")'], calls


def _normalize_bash(block: str) -> list[str]:
    """Strip trailing # comments and quoted-string contents so translations don't break comparison."""
    out: list[str] = []
    for line in block.splitlines():
        line = re.sub(r"(?<!\\)#.*$", "", line).rstrip()
        if not line:
            continue
        line = re.sub(r'"[^"]*"', '""', line)
        out.append(line)
    return out


def test_command_lines_in_code_blocks_match_across_readmes():
    en_blocks = _fences(_lines(EN))
    ja_blocks = _fences(_lines(JA))
    bash_en = [b for b in en_blocks if "python -m account_keeper" in b or "ollama " in b]
    bash_ja = [b for b in ja_blocks if "python -m account_keeper" in b or "ollama " in b]
    norm_en = [_normalize_bash(b) for b in bash_en]
    norm_ja = [_normalize_bash(b) for b in bash_ja]
    assert norm_en == norm_ja, \
        "bash commands (ignoring translated comments and localized quoted args) must match across READMEs"


def test_no_module_listens_on_any_port():
    sources = {str(p.relative_to(ROOT / "account_keeper")): p.read_text(encoding="utf-8")
               for p in (ROOT / "account_keeper").rglob("*.py")}
    assert sources
    for name, src in sources.items():
        match = LISTEN_RE.search(src)
        assert match is None, f"{name} would open a listening socket: {match}"
    for name, src in sources.items():
        for line in src.splitlines():
            if not any(str(port) in line for port in FORBIDDEN_PORTS):
                continue
            assert any(hint in line for hint in PORT_HINTS), \
                f"{name} mentions a forbidden port outside a client URL: {line}"


def test_example_configs_launch_stdio_without_ports():
    for href in ("examples/hermes-config.yaml", "examples/claude-code.mcp.json"):
        text = (ROOT / href).read_text(encoding="utf-8")
        assert "account_keeper.mcp_server" in text, href
        non_comment = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        assert not any(str(port) in non_comment for port in FORBIDDEN_PORTS), href
        for url in re.findall(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S*", non_comment):
            assert not any(f":{port}" in url for port in FORBIDDEN_PORTS), \
                f"{href} launches a network endpoint on a forbidden port: {url}"
