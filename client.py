"""Bounded Anysearch JSON-RPC client shared by commands and AI tools."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import aiohttp


class SearchError(RuntimeError):
    """An error safe to show in chat, without response bodies or secrets."""

    def __init__(self, message: str, *, code: str = "", request_id: str = "") -> None:
        self.code = code
        self.request_id = request_id
        if code:
            message += f"（{code}）"
        if request_id:
            message += f"\n请求编号：{request_id}"
        super().__init__(message)


def _safe_detail(text: str, redactions: tuple[str, ...]) -> str:
    """Keep a short service explanation, redact credentials and request inputs."""
    for value in sorted(set(redactions), key=len, reverse=True):
        if value:
            text = text.replace(value, "[已隐藏]")
    text = re.sub(
        r"(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+[^\s,;\"']+",
        "[鉴权信息已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}&]+)",
        r"\1[已隐藏]",
        text,
    )
    text = re.sub(r"(?im)\b(?:set-cookie|cookie)\s*[:=]\s*[^\r\n]+", "Cookie=[已隐藏]", text)
    text = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
        "[已隐藏]",
        text,
    )
    text = re.sub(r"https?://[^\s<>\"']+", "[URL已省略]", text)
    text = re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", " ", text)
    return " ".join(text.split())[:240]


def _service_error(data: dict[str, Any], redactions: tuple[str, ...]) -> SearchError:
    result = data.get("result")
    result = result if isinstance(result, dict) else {}
    error = data.get("error")
    code, detail = "", ""
    if isinstance(error, dict):
        value = error.get("code", "")
        if isinstance(value, (int, str)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(value)):
            code = str(value)
        if isinstance(error.get("message"), str):
            detail = error["message"]
    elif isinstance(error, str):
        detail = error
    else:
        content = result.get("content", [])
        if isinstance(content, list):
            detail = "\n".join(
                item["text"][:4096]
                for item in content[:2]
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            )
        lines = detail.strip().splitlines()
        if len(lines) > 1 and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", lines[0].strip()):
            code = lines[0].strip()
            detail = "\n".join(lines[1:])
    explanations = {
        "extract_failed": "网页正文提取失败：服务无法读取此链接。可能与网站访问限制、登录要求或页面不可用有关。",
        "extract_timeout": "网页正文提取超时，请稍后重试。",
        "timeout": "搜索服务处理请求超时，请稍后重试。",
        "invalid_api_key": "API Key 无效，请检查插件配置。",
        "unauthorized": "搜索服务鉴权失败，请检查 API Key。",
        "quota_exceeded": "搜索服务额度不足，请检查账户额度。",
        "rate_limit_exceeded": "搜索服务请求过于频繁，请稍后重试。",
        "-32602": "搜索服务拒绝了请求参数。",
        "-32601": "搜索服务不支持此调用。",
    }
    message = explanations.get(code.lower(), "搜索服务未能完成请求。")
    safe_detail = _safe_detail(detail, redactions)
    if safe_detail:
        message += f"\n服务说明：{safe_detail}"
    meta = result.get("_meta", data.get("_meta", {}))
    request_id = meta.get("request_id", "") if isinstance(meta, dict) else ""
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-fA-F0-9-]{16,64}", request_id):
        request_id = ""
    if any(value and value in code for value in redactions):
        code = ""
    if any(value and value in request_id for value in redactions):
        request_id = ""
    return SearchError(message, code=code, request_id=request_id)


def _request_redactions(arguments: dict[str, Any]) -> tuple[str, ...]:
    values = []
    for value in arguments.values():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    values.extend(_request_redactions(item))
    return tuple(values)


def decode_response(text: str, *, redactions: tuple[str, ...] = ()) -> str:
    """Accept JSON and MCP SSE; preserve every text content block."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
        for frame in text.replace("\r\n", "\n").split("\n\n"):
            payload = "\n".join(
                line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
            )
            if not payload:
                continue
            try:
                candidate = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                data = candidate
        if data is None:
            raise SearchError("搜索服务返回了无法解析的内容，请稍后重试。")
    if not isinstance(data, dict):
        raise SearchError("搜索服务返回的数据格式不正确，请稍后重试。")
    if "error" in data:
        raise _service_error(data, redactions)
    result = data.get("result")
    if not isinstance(result, dict):
        raise SearchError("搜索服务未返回有效结果，请稍后重试。")
    if result.get("isError"):
        raise _service_error(data, redactions)
    content = result.get("content", [])
    if not isinstance(content, list):
        raise SearchError("搜索服务返回的数据格式不正确，请稍后重试。")
    blocks = [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    if blocks:
        return "\n\n".join(blocks)
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)) and structured:
        return json.dumps(structured, ensure_ascii=False, indent=2)
    raise SearchError("搜索服务没有返回可读内容，请换个关键词或网页地址。")


class SearchClient:
    endpoint = "https://api.anysearch.com/mcp"
    response_limit = 2 * 1024 * 1024

    def __init__(
        self,
        api_key: str = "",
        *,
        timeout: int = 30,
        cache_ttl: int = 60,
        max_concurrency: int = 3,
        on_error: Callable[[str, SearchError], None] | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._on_error = on_error
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._pending: dict[str, asyncio.Task[str]] = {}
        self._closed = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, name: str, arguments: dict[str, Any]) -> str:
        async with self._semaphore:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    trust_env=True,
                )
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            # Retry temporary server errors once; never retry quota/auth errors.
            for attempt in range(2):
                try:
                    async with self._session.post(
                        self.endpoint,
                        json=payload,
                        headers=self._headers(),
                    ) as response:
                        status = response.status
                        if status in {502, 503, 504} and attempt == 0:
                            await asyncio.sleep(0.5)
                            continue
                        if status >= 400:
                            hints = {
                                401: "API Key 无效或未获授权，请检查插件配置。",
                                403: "搜索服务拒绝访问，请检查 API Key 或账户权限。",
                                429: "搜索服务额度不足或请求过于频繁，请稍后重试或配置 API Key。",
                            }
                            raise SearchError(
                                hints.get(
                                    status, f"搜索服务暂时不可用（HTTP {status}），请稍后重试。"
                                )
                            )
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > self.response_limit:
                                raise SearchError(
                                    "搜索服务返回的内容过大，请减少结果数或换个网页。"
                                )
                        return decode_response(
                            body.decode("utf-8", errors="replace"),
                            redactions=(self.api_key, *_request_redactions(arguments)),
                        )
                except TimeoutError as exc:
                    raise SearchError(
                        "搜索请求超时，请稍后重试；也可在配置中增加超时时间。"
                    ) from exc
                except aiohttp.ClientError as exc:
                    raise SearchError(
                        "无法连接搜索服务，请检查 AstrBot 的网络或代理设置。"
                    ) from exc
        raise SearchError("搜索服务暂时不可用，请稍后重试。")

    async def _fetch(self, key: str, name: str, arguments: dict[str, Any]) -> str:
        try:
            result = await self._request(name, arguments)
            if self.cache_ttl > 0 and len(result.encode("utf-8")) <= 128 * 1024:
                self._cache[key] = (time.monotonic() + self.cache_ttl, result)
                self._cache.move_to_end(key)
                while len(self._cache) > 64:
                    self._cache.popitem(last=False)
            return result
        except SearchError as exc:
            if self._on_error is not None:
                self._on_error(name, exc)
            raise
        finally:
            self._pending.pop(key, None)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._closed:
            raise SearchError("插件正在卸载，请稍后重试。")
        key = json.dumps([name, arguments], ensure_ascii=False, sort_keys=True)
        cached = self._cache.get(key)
        if cached:
            if cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return cached[1]
            del self._cache[key]
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= 32:
                raise SearchError("当前搜索请求较多，请稍后再试。")
            task = asyncio.create_task(self._fetch(key, name, arguments))
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self._pending[key] = task
        return await asyncio.shield(task)

    async def search(self, query: str, max_results: int) -> str:
        return await self.call("search", {"query": query, "max_results": max_results})

    async def batch_search(self, queries: list[dict[str, Any]]) -> str:
        return await self.call("batch_search", {"queries": queries})

    async def extract(self, url: str) -> str:
        return await self.call("extract", {"url": url})

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._cache.clear()
        if self._session is not None:
            await self._session.close()
