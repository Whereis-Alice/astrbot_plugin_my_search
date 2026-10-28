"""Shared validation and output limits; no AstrBot dependencies."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .client import SearchClient, SearchError


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(number, maximum))


def is_public_http_url(url: str) -> bool:
    """Reject obvious non-public targets; remote-service DNS is outside our control."""
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        if parsed.scheme not in {"http", "https"} or not host or port == 0:
            return False
        if parsed.username is not None or parsed.password is not None or "%" in host:
            return False
        if host == "localhost" or host.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home", ".localdomain")
        ):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or all(
                re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", part) for part in host.split(".")
            ):
                return False
            ascii_host = host.encode("idna").decode("ascii")
            return len(ascii_host) <= 253 and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in ascii_host.split(".")
            )
        return ip.is_global and not ip.is_multicast
    except (ValueError, UnicodeError):
        return False


def normalize_query(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchError("请填写要搜索的关键词，例如：/搜一搜 AstrBot 安装教程")
    query = value.strip()
    if len(query) > 1000:
        raise SearchError("搜索关键词不能超过 1000 个字符，请精简后再试。")
    return query


def parse_queries(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SearchError(
                    "批量搜索的 JSON 数组格式不正确。也可用中文分号分隔关键词。"
                ) from exc
        else:
            value = re.split(r"[；;，,\n]+", text)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SearchError("批量搜索需要关键词列表，例如：/一起搜 AstrBot；NapCat")
    queries = list(dict.fromkeys(normalize_query(item) for item in value if item.strip()))
    if not queries:
        raise SearchError("请填写关键词，例如：/一起搜 AstrBot；NapCat")
    if len(queries) > 5:
        raise SearchError("一次最多搜索 5 个不同的关键词，请分批发送。")
    return queries


def clean_result(text: str) -> str:
    return re.sub(r"\n*```ya?ml\s*\nrequest_id:[^\n]*\n```\s*$", "\n", text).strip()


def limit_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    notice = "\n\n（内容较长，已截断；可缩小搜索范围，或在插件配置中提高输出上限。）"
    return text[: max(0, limit - len(notice))].rstrip() + notice


def split_text(text: str, size: int) -> list[str]:
    """Prefer paragraph/line boundaries, preserve all characters and enforce the cap."""
    if size < 1:
        raise ValueError("chunk size must be positive")
    chunks = []
    while len(text) > size:
        end = text.rfind("\n\n", 0, size)
        if end >= size // 3:
            end = min(end + 2, size)
        else:
            end = text.rfind("\n", 0, size)
            end = end + 1 if end >= size // 3 else size
        chunks.append(text[:end])
        text = text[end:]
    if text:
        chunks.append(text)
    return chunks


class SearchService:
    def __init__(
        self, client: SearchClient, *, default_results: int = 5, tool_limit: int = 12000
    ) -> None:
        self.client = client
        self.default_results = default_results
        self.tool_limit = tool_limit

    async def search(self, query: str, max_results: int | None = None) -> str:
        query = normalize_query(query)
        count = bounded_int(max_results, self.default_results, 1, 10)
        return clean_result(await self.client.search(query, count))

    async def extract(self, url: str) -> str:
        url = url.strip() if isinstance(url, str) else ""
        if not is_public_http_url(url):
            raise SearchError(
                "请提供公开的 http(s) 网页地址；不支持本地、内网、带账号密码或格式错误的地址。"
            )
        return clean_result(await self.client.extract(url))

    async def batch(self, queries: str | list[str], max_results: int | None = None) -> str:
        items = parse_queries(queries)
        count = bounded_int(max_results, self.default_results, 1, 10)
        payload = [{"query": query, "max_results": count} for query in items]
        return clean_result(await self.client.batch_search(payload))
