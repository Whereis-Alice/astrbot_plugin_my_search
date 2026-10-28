"""我的搜索: command routing, configuration and message delivery."""

from collections.abc import AsyncIterator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .client import SearchClient, SearchError
from .search_service import SearchService, bounded_int, is_public_http_url, limit_text, split_text
from .tools import MyBatchSearchTool, MyExtractTool, MySearchTool


class MySearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        mode = config.get("result_mode", "合并转发")
        mode = {"合并转发": "forward", "长结果转发": "auto", "分段文本": "text"}.get(mode, mode)
        self.result_mode = mode if mode in {"forward", "auto", "text"} else "forward"
        self.forward_private = self._bool("forward_private", False)
        self.forward_threshold = self._int("forward_threshold", 1000, 100, 10000)
        self.chunk_size = self._int("chunk_size", 1000, 300, 3000)
        self.command_limit = self._int("command_output_limit", 6000, 500, 60000)
        self.forward_name = str(config.get("forward_name", "我的搜索")).strip()[:32] or "我的搜索"
        self.client = SearchClient(
            api_key=str(config.get("api_key", "")),
            timeout=self._int("request_timeout", 30, 5, 120),
            cache_ttl=self._int("cache_ttl", 60, 0, 3600),
            max_concurrency=self._int("max_concurrency", 3, 1, 10),
        )
        self.service = SearchService(
            self.client,
            default_results=self._int("max_results", 5, 1, 10),
            tool_limit=self._int("tool_output_limit", 12000, 500, 60000),
        )
        self.tools = [
            MySearchTool(service=self.service),
            MyExtractTool(service=self.service),
            MyBatchSearchTool(service=self.service),
        ]
        if self._bool("enable_ai_tools", True):
            self.context.add_llm_tools(*self.tools)

    def _int(self, key: str, default: int, low: int, high: int) -> int:
        return bounded_int(self.config.get(key), default, low, high)

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            value = value.strip().lower()
            if value in {"true", "1", "on", "yes"}:
                return True
            if value in {"false", "0", "off", "no"}:
                return False
        return default

    @staticmethod
    def usage() -> str:
        return (
            "我的搜索\n"
            "/搜一搜 关键词 —— 搜索网页资料\n"
            "/看网页 https://example.com/page —— 提取网页正文\n"
            "/一起搜 关键词一；关键词二 —— 最多 5 个关键词\n"
            "/搜一搜 帮助 —— 查看用法\n"
            "也可用 /搜一搜 提取 URL、/搜一搜 批量 关键词一；关键词二。\n"
            "斜杠是示例唤醒前缀，请按你的 AstrBot 设置使用。"
        )

    @filter.command("搜一搜", alias={"我的搜索"}, desc="搜索公开网页资料；输入“帮助”查看用法。")
    async def my_search_command(
        self, event: AstrMessageEvent, query: GreedyStr
    ) -> AsyncIterator[MessageEventResult]:
        event.should_call_llm(False)
        query = query.strip()
        if not query or query in {"帮助", "help", "用法"}:
            yield event.plain_result(self.usage()).use_t2i(False)
            return
        parts = query.split(maxsplit=1)
        action = parts[0]
        remainder = parts[1] if len(parts) == 2 else ""
        if action in {"提取", "网页"}:
            operation, value = "extract", remainder
        elif action == "批量":
            operation, value = "batch", remainder
        elif is_public_http_url(query):
            operation, value = "extract", query
        else:
            operation, value = "search", query
        async for result in self._execute(event, operation, value):
            yield result

    @filter.command("看网页", alias={"我的网页"}, desc="提取公开网页正文。")
    async def my_extract_command(
        self, event: AstrMessageEvent, url: GreedyStr
    ) -> AsyncIterator[MessageEventResult]:
        event.should_call_llm(False)
        async for result in self._execute(event, "extract", url):
            yield result

    @filter.command("一起搜", alias={"我的批搜"}, desc="用分号分隔关键词，一次最多搜索 5 项。")
    async def my_batch_command(
        self, event: AstrMessageEvent, queries: GreedyStr
    ) -> AsyncIterator[MessageEventResult]:
        event.should_call_llm(False)
        async for result in self._execute(event, "batch", queries):
            yield result

    async def _execute(
        self, event: AstrMessageEvent, operation: str, value: str
    ) -> AsyncIterator[MessageEventResult]:
        try:
            if operation == "extract":
                result = await self.service.extract(value)
            elif operation == "batch":
                result = await self.service.batch(value)
            else:
                result = await self.service.search(value)
        except SearchError as exc:
            yield event.plain_result(f"我的搜索：{exc}").use_t2i(False)
            return
        await self._send_result(event, result)

    def _use_forward(self, event: AstrMessageEvent, text: str) -> bool:
        if event.get_platform_name() != "aiocqhttp" or self.result_mode == "text":
            return False
        if not event.get_group_id() and not self.forward_private:
            return False
        return self.result_mode == "forward" or len(text) >= self.forward_threshold

    async def _send_result(self, event: AstrMessageEvent, text: str) -> None:
        text = limit_text(text, self.command_limit)
        chunks = split_text(text, self.chunk_size - 20) or [
            "未找到可读结果，请换个关键词或网页地址。"
        ]
        if self._use_forward(event, text):
            try:
                sender = str(event.get_self_id())
                if not sender.isdigit() or int(sender) <= 0:
                    raise ValueError("missing OneBot self ID")
                nodes = [
                    Node(uin=sender, name=self.forward_name, content=[Plain(chunk)])
                    for chunk in chunks
                ]
                await event.send(MessageChain([Nodes(nodes)]))
                return
            except Exception as exc:  # noqa: BLE001 - exception types vary by protocol implementation.
                # Protocol exceptions may contain user content; only log their type.
                logger.warning(
                    "我的搜索：合并转发发送失败（%s），改用分段文本。", type(exc).__name__
                )
                chunks = split_text(
                    "合并转发未成功，以下改用分段文本。\n\n" + "".join(chunks), self.chunk_size - 20
                )
        # Bypass global long-text-to-forward decoration so fallback never repeats
        # the failing forward action. Commands keep source links as plain text.
        total = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            label = f"[{index}/{total}]\n" if total > 1 else ""
            await event.send(MessageChain([Plain(label + chunk)]))

    async def terminate(self) -> None:
        try:
            tool_set = self.context.get_llm_tool_manager()
            for tool in self.tools:
                if tool_set.get_tool(tool.name) is tool:
                    tool_set.remove_tool(tool.name)
        finally:
            await self.client.close()
        logger.info("我的搜索已卸载。")
