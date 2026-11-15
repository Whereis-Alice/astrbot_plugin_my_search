"""Small QQ Markdown subset with intact links and bounded reply pages."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from .search_service import is_public_http_url

_LINK_START = re.compile(r"(!?)\[([^\]\n]*)\]\(|https?://", re.IGNORECASE)
_ESCAPE = re.compile(r"([\\`*_\[\]<>#])")
_NOTICE = "\n\n（结果较长，已截断；可缩小搜索范围，或调整输出上限和 QQ 官 bot 回复条数。）"


@dataclass
class QQPage:
    markdown: str
    plain: str


def _escape(text: str) -> str:
    return _ESCAPE.sub(r"\\\1", text)


def _target(url: str) -> str | None:
    if not is_public_http_url(url):
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    # Encode parentheses so a URL never closes the Markdown link prematurely.
    return urlunsplit(
        (
            parsed.scheme,
            host,
            quote(parsed.path, safe="/:%@!$&*+,;=-._~"),
            quote(parsed.query, safe="/?:%@!$&*+,;=-._~"),
            quote(parsed.fragment, safe="/?:%@!$&*+,;=-._~"),
        )
    )


def _inline(text: str, show_links: bool) -> Iterator[tuple[str, str, bool]]:
    """Yield escaped text characters and indivisible Markdown/plain link pairs."""
    cursor = 0
    while match := _LINK_START.search(text, cursor):
        for char in text[cursor : match.start()]:
            yield _escape(char), char, False
        if match.group(2) is not None:
            start, end, depth = match.end(), match.end(), 1
            while end < len(text) and depth:
                if text[end] == "(":
                    depth += 1
                elif text[end] == ")":
                    depth -= 1
                end += 1
            if depth:
                # Do not invent a target from an incomplete Markdown link.
                for char in match.group(2) + "（不完整的链接已省略）":
                    yield _escape(char), char, False
                yield from _inline(text[match.end() :], False)
                return
            url = text[start : end - 1].strip().strip("<>")
            label = match.group(2).strip() or "查看来源"
            if match.group(1):
                label = "图片：" + label
        else:
            end = match.end()
            while end < len(text) and not re.match(r'[\s<>"\[\]]', text[end]):
                end += 1
            url = text[match.start() : end]
            trimmed = url.rstrip(".,;，。；！")
            while trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
                trimmed = trimmed[:-1]
            end -= len(url) - len(trimmed)
            url, label = trimmed, "查看来源"
            # An autolink's angle brackets are ordinary escaped text otherwise.
        target = _target(url)
        if not show_links:
            md = plain = "（链接已隐藏）"
        elif target is None:
            md = plain = "（不支持的链接已省略）"
        else:
            label = re.sub(r"[*_`]+", "", label)
            label = label[:99] + "…" if len(label) > 100 else label
            md, plain = f"[{_escape(label)}]({target})", f"{label}：{target}"
        yield md, plain, True
        cursor = end
    for char in text[cursor:]:
        yield _escape(char), char, False


def _tokens(text: str, show_links: bool) -> Iterator[tuple[str, str, bool]]:
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"^\s*```", line):
            continue
        if re.match(r"^## Search Results\b", line):
            continue
        line = re.sub(r"\*\*([^\n]*?)\*\*", r"\1", line).replace("`", "")
        line = re.sub(r"^(-\s*)?URL\s*:", "来源：", line, flags=re.IGNORECASE)
        line = re.sub(r"<(https?://[^<>\s]+)>", r"\1", line, flags=re.IGNORECASE)
        heading = re.match(r"^#{1,6}\s+(.+)", line)
        if heading:
            yield "## ", "", False
            line = heading.group(1)
        elif re.match(r"^[-*]\s+", line):
            yield "- ", "- ", False
            line = line[2:]
        yield from _inline(line, show_links)
        yield "\n", "\n", False


def hide_page_links(page: QQPage) -> QQPage:
    """Hide links on this page without moving content between reply pages."""

    def hide(text: str) -> str:
        return "".join(plain for _, plain, _ in _inline(text, False))

    return QQPage(hide(page.markdown), hide(page.plain))


def build_qq_pages(
    text: str,
    *,
    title: str = "搜索结果",
    show_links: bool = True,
    chunk_size: int = 1000,
    max_messages: int = 3,
    output_limit: int = 6000,
) -> tuple[list[QQPage], bool]:
    """Bound both formats together; never cut or silently retarget a source link."""
    chunk_size = min(chunk_size, 2000)
    header_size = len(f"## 我的搜索 · {title}（{max_messages}/{max_messages}）\n\n")
    body_size = chunk_size - header_size - len(_NOTICE)
    budget = output_limit - header_size * max_messages - len(_NOTICE)
    if body_size < 1 or budget < 1:
        raise ValueError("QQ output limits are too small")
    bodies: list[QQPage] = []
    md_parts: list[str] = []
    plain_parts: list[str] = []
    used = page_used = 0
    truncated = False
    for md, plain, atomic in _tokens(text, show_links):
        cost = max(len(md), len(plain))
        if atomic and cost > body_size:
            md = plain = "（链接过长，已省略）"
            cost = len(md)
        if used + cost > budget:
            truncated = True
            break
        if page_used + cost > body_size:
            bodies.append(QQPage("".join(md_parts), "".join(plain_parts)))
            md_parts, plain_parts, page_used = [], [], 0
            if len(bodies) == max_messages:
                truncated = True
                break
        md_parts.append(md)
        plain_parts.append(plain)
        page_used += cost
        used += cost
    if md_parts:
        bodies.append(QQPage("".join(md_parts), "".join(plain_parts)))
    if not bodies:
        bodies = [QQPage("未找到可读结果。", "未找到可读结果。")]
    pages = []
    for index, body in enumerate(bodies, 1):
        count = f"（{index}/{len(bodies)}）" if len(bodies) > 1 else ""
        heading = f"我的搜索 · {title}{count}\n\n"
        notice = _NOTICE if truncated and index == len(bodies) else ""
        pages.append(
            QQPage(
                "## " + heading + body.markdown.rstrip() + notice,
                heading + body.plain.rstrip() + notice,
            )
        )
    return pages, truncated
