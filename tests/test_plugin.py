"""Offline integration tests with actual AstrBot SDK and local HTTP/OneBot endpoints."""

import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.astrbot_message import Group
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.filter.command import CommandFilter, GreedyStr

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("my_search_test_plugin")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
client_module = importlib.import_module(package.__name__ + ".client")
service_module = importlib.import_module(package.__name__ + ".search_service")
plugin_module = importlib.import_module(package.__name__ + ".main")
SearchClient = client_module.SearchClient
SearchError = client_module.SearchError
SearchService = service_module.SearchService
MySearchPlugin = plugin_module.MySearchPlugin


class LocalBot:
    def __init__(self, fail_forward=False):
        self.fail_forward = fail_forward
        self.actions = []

    async def call_action(self, action, **payload):
        self.actions.append((action, payload))
        if self.fail_forward:
            raise RuntimeError("simulated protocol failure with confidential payload")

    async def send_group_msg(self, **payload):
        self.actions.append(("send_group_msg", payload))

    async def send_private_msg(self, **payload):
        self.actions.append(("send_private_msg", payload))


class LocalEvent(AstrMessageEvent):
    def __init__(self, *, platform="aiocqhttp", group="123", fail_forward=False, message=""):
        obj = SimpleNamespace(
            type=MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE,
            self_id="456",
            sender=SimpleNamespace(user_id="789", nickname="Tester"),
            group=Group(group_id=group) if group else None,
            group_id=group,
            message=[Plain(message)],
            message_str=message,
            raw_message=None,
            message_id="42",
        )
        meta = SimpleNamespace(name=platform, id="test", description="local test")
        super().__init__(message, obj, meta, group or "789")
        self.bot = LocalBot(fail_forward)
        self.sent = []
        self.is_at_or_wake_command = True

    async def send(self, chain):
        self.sent.append(chain)
        if self.get_platform_name() == "aiocqhttp":
            await AiocqhttpMessageEvent.send_message(
                self.bot,
                chain,
                is_group=bool(self.get_group_id()),
                session_id=self.get_group_id() or self.get_sender_id(),
            )


class LocalContext:
    def __init__(self):
        self.tools = ToolSet()

    def add_llm_tools(self, *tools):
        for tool in tools:
            self.tools.add_tool(tool)

    def get_llm_tool_manager(self):
        return self.tools

    def get_config(self, *args):
        return {}


class ValidationTests(unittest.TestCase):
    def test_public_urls_and_malformed_urls(self):
        for url in [
            "https://example.com/a",
            "http://8.8.8.8/",
            "https://[2606:4700:4700::1111]/",
            "https://例子.测试/",
        ]:
            with self.subTest(url=url):
                self.assertTrue(service_module.is_public_http_url(url))
        for url in [
            "http://localhost",
            "http://foo.localhost",
            "http://a.local",
            "http://a.internal",
            "http://127.0.0.1",
            "http://10.1.2.3",
            "http://100.64.0.1",
            "http://224.0.0.1",
            "http://[::1]",
            "http://[::ffff:127.0.0.1]",
            "http://2130706433",
            "http://0x7f000001",
            "http://127.1",
            "http://user:pass@example.com",
            "https://example.com:bad",
            "http://[bad",
            "http://example.com:0",
            "file:///a",
            "https://example.com\\@localhost",
            "https://exa mple.com",
        ]:
            with self.subTest(url=url):
                self.assertFalse(service_module.is_public_http_url(url))

    def test_batch_validation(self):
        parse = service_module.parse_queries
        self.assertEqual(parse("a；b,a\nc"), ["a", "b", "c"])
        self.assertEqual(parse('["a,b", "c"]'), ["a,b", "c"])
        for value in ["", "[bad", [1], {"query": "a"}, ["x" * 1001], "a;b;c;d;e;f"]:
            with self.subTest(value=value), self.assertRaises(SearchError):
                parse(value)

    def test_chunk_boundaries_preserve_content(self):
        for text in ["x" * 12000, "字\n" * 900, ("hello\n\n" * 700), "x" * 99 + "\n" + "end", ""]:
            chunks = service_module.split_text(text, 100)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(0 < len(chunk) <= 100 for chunk in chunks))
        with self.assertRaises(ValueError):
            service_module.split_text("abc", 0)

    def test_output_limit_and_metadata(self):
        self.assertEqual(service_module.limit_text("abc", 500), "abc")
        clipped = service_module.limit_text("x" * 1000, 500)
        self.assertLessEqual(len(clipped), 500)
        self.assertIn("已截断", clipped)
        text = "result https://example.com\n\n```yaml\nrequest_id: abc\n```\n"
        self.assertEqual(service_module.clean_result(text), "result https://example.com")

    def test_json_and_sse_errors_and_multiple_blocks(self):
        valid = {
            "result": {
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "image"},
                    {"type": "text", "text": "b"},
                ]
            }
        }
        text = json.dumps(valid)
        self.assertEqual(client_module.decode_response(text), "a\n\nb")
        self.assertEqual(
            client_module.decode_response("event: message\r\ndata: " + text + "\r\n\r\n"), "a\n\nb"
        )
        for invalid in [
            "[]",
            "null",
            "not json",
            "{}",
            '{"error":{"message":"secret"}}',
            '{"result":{"isError":true,"content":[{"type":"text","text":"secret"}]}}',
            '{"result":{"content":{}}}',
            '{"result":{"content":[]}}',
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(SearchError) as caught:
                client_module.decode_response(invalid)
            self.assertNotIn("secret", str(caught.exception))


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = LocalContext()
        self.plugin = MySearchPlugin(self.context, {})
        self.plugin.client.search = AsyncMock(return_value="search source https://example.com")
        self.plugin.client.extract = AsyncMock(return_value="page body https://example.com")
        self.plugin.client.batch_search = AsyncMock(return_value="batch sources")

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_group_forward_uses_real_adapter_nodes(self):
        event = LocalEvent()
        text = "first\n\n" + "x" * 1900
        await self.plugin._send_result(event, text)
        self.assertEqual(len(event.bot.actions), 1)
        action, payload = event.bot.actions[0]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(payload["group_id"], "123")
        self.assertEqual(
            "".join(node["data"]["content"][0]["data"]["text"] for node in payload["messages"]),
            text,
        )
        self.assertEqual(payload["messages"][0]["data"]["user_id"], "456")
        self.assertEqual(payload["messages"][0]["data"]["nickname"], "我的搜索")

    async def test_forward_failure_falls_back_without_repeat(self):
        event = LocalEvent(fail_forward=True)
        await self.plugin._send_result(event, "x" * 1900)
        actions = [action for action, _ in event.bot.actions]
        self.assertEqual(actions.count("send_group_forward_msg"), 1)
        self.assertGreater(actions.count("send_group_msg"), 0)
        self.assertIn("改用分段文本", event.sent[1].chain[0].text)
        self.assertTrue(
            all(len(chain.chain[0].text) <= self.plugin.chunk_size for chain in event.sent[1:])
        )

    async def test_private_default_text_and_opt_in_forward(self):
        event = LocalEvent(group="")
        await self.plugin._send_result(event, "hello")
        self.assertEqual(event.bot.actions[0][0], "send_private_msg")
        self.plugin.forward_private = True
        event = LocalEvent(group="")
        await self.plugin._send_result(event, "hello")
        self.assertEqual(event.bot.actions[0][0], "send_private_forward_msg")
        self.assertEqual(event.bot.actions[0][1]["user_id"], "789")

    async def test_auto_threshold_text_mode_other_platform_and_missing_id(self):
        self.plugin.result_mode = "auto"
        event = LocalEvent()
        await self.plugin._send_result(event, "short")
        self.assertEqual(event.bot.actions[0][0], "send_group_msg")
        await self.plugin._send_result(event, "x" * 1000)
        self.assertEqual(event.bot.actions[1][0], "send_group_forward_msg")
        self.plugin.result_mode = "text"
        self.assertFalse(self.plugin._use_forward(event, "x" * 5000))
        self.plugin.result_mode = "forward"
        for platform in ["telegram", "qq_official", "lark"]:
            event = LocalEvent(platform=platform)
            await self.plugin._send_result(event, "x" * 2100)
            self.assertTrue(all(isinstance(chain.chain[0], Plain) for chain in event.sent))
        event = LocalEvent()
        event.message_obj.self_id = ""
        await self.plugin._send_result(event, "hello")
        self.assertEqual(event.bot.actions[0][0], "send_group_msg")

    async def test_actual_command_filter_empty_multiword_and_alias(self):
        cmd = CommandFilter(
            "搜一搜",
            alias={"我的搜索"},
            handler_md=SimpleNamespace(handler=MySearchPlugin.my_search_command),
        )
        self.assertIs(cmd.handler_params["query"], GreedyStr)
        for message, query in [
            ("搜一搜", ""),
            ("搜一搜 hello world", "hello world"),
            ("我的搜索 hello world", "hello world"),
        ]:
            event = LocalEvent(message=message)
            self.assertTrue(cmd.filter(event, {}))
            self.assertEqual(event.get_extra("parsed_params")["query"], query)
        self.assertFalse(cmd.filter(LocalEvent(message="anysearch test"), {}))
        event = LocalEvent()
        results = [result async for result in self.plugin.my_search_command(event, "")]
        self.assertIn("我的搜索", results[0].chain[0].text)
        self.plugin.client.search.assert_not_awaited()

    async def test_all_command_routes_and_no_extra_llm(self):
        event = LocalEvent()
        for query in [
            "hello world",
            "提取 https://example.com",
            "批量 a；b",
            "https://example.com",
        ]:
            self.assertEqual([r async for r in self.plugin.my_search_command(event, query)], [])
        self.plugin.client.search.assert_awaited_once_with("hello world", 5)
        self.assertEqual(self.plugin.client.extract.await_count, 2)
        self.plugin.client.batch_search.assert_awaited_once_with(
            [{"query": "a", "max_results": 5}, {"query": "b", "max_results": 5}]
        )
        [r async for r in self.plugin.my_extract_command(event, "https://example.com")]
        [r async for r in self.plugin.my_batch_command(event, "a;b")]
        self.assertFalse(event.call_llm)

    async def test_errors_no_network_and_all_tools_call(self):
        event = LocalEvent()
        result = [r async for r in self.plugin.my_extract_command(event, "http://localhost")]
        self.assertIn("公开", result[0].chain[0].text)
        self.plugin.client.extract.assert_not_awaited()
        search, extract, batch = self.plugin.tools
        self.assertEqual(search.name, "my_search_search")
        self.assertIn("source", await search.call(None, "test", 100))
        self.plugin.client.search.assert_awaited_once_with("test", 10)
        self.assertIn("page", await extract.call(None, "https://example.com"))
        self.assertIn("batch", await batch.call(None, ["a", "b"]))
        self.assertIn("错误", await batch.call(None, ["a"] * 6 + ["b", "c", "d", "e", "f"]))
        self.plugin.client.search.side_effect = SearchError("timeout")
        self.assertIn("错误", await search.call(None, "bad"))

    async def test_config_bounds_disable_tools_and_unload_keeps_other_tools(self):
        other = MySearchPlugin(
            self.context,
            {
                "enable_ai_tools": False,
                "chunk_size": "bad",
                "request_timeout": -1,
                "max_results": 999,
                "forward_private": "false",
            },
        )
        self.assertEqual(other.chunk_size, 1000)
        self.assertEqual(other.client.timeout, 5)
        self.assertEqual(other.service.default_results, 10)
        self.assertFalse(other.forward_private)
        for choice, expected in [
            ("合并转发", "forward"),
            ("长结果转发", "auto"),
            ("分段文本", "text"),
        ]:
            configured = MySearchPlugin(LocalContext(), {"result_mode": choice})
            self.assertEqual(configured.result_mode, expected)
            await configured.terminate()
        await other.terminate()
        self.assertEqual(len(self.context.tools.tools), 3)
        replacement = type(self.plugin.tools[0])(service=self.plugin.service)
        self.context.tools.add_tool(replacement)
        await self.plugin.terminate()
        self.assertIs(self.context.tools.get_tool(replacement.name), replacement)
        self.assertTrue(self.plugin.client._closed)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.statuses = []
        self.payload = {"result": {"content": [{"type": "text", "text": "ok"}]}}
        self.raw = None
        self.delay = 0
        self.active = self.peak = 0

        async def endpoint(request):
            self.calls.append(await request.json())
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                status = self.statuses.pop(0) if self.statuses else 200
                if self.raw is not None:
                    return web.Response(text=self.raw, status=status)
                return web.json_response(self.payload, status=status)
            finally:
                self.active -= 1

        app = web.Application()
        app.router.add_post("/mcp", endpoint)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        server = await asyncio.get_running_loop().create_server(self.runner.server, "127.0.0.1", 0)
        self.server = server
        self.client = SearchClient(cache_ttl=60, max_concurrency=2)
        self.client.endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp"

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()
        await self.runner.cleanup()

    async def test_cache_coalescing_and_zero_ttl(self):
        results = await asyncio.gather(*(self.client.search("same", 5) for _ in range(8)))
        self.assertEqual(results, ["ok"] * 8)
        self.assertEqual(len(self.calls), 1)
        await self.client.search("same", 5)
        self.assertEqual(len(self.calls), 1)
        self.client.cache_ttl = 0
        self.client._cache.clear()
        await self.client.search("same", 5)
        await self.client.search("same", 5)
        self.assertEqual(len(self.calls), 3)

    async def test_concurrency_and_waiter_cancellation(self):
        self.delay = 0.04
        await asyncio.gather(*(self.client.search(str(i), 5) for i in range(8)))
        self.assertEqual(self.peak, 2)
        waiter = asyncio.create_task(self.client.search("cancelled", 5))
        survivor = asyncio.create_task(self.client.search("cancelled", 5))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(await survivor, "ok")

    async def test_http_retry_auth_quota_no_error_caching(self):
        self.statuses = [503, 200]
        self.assertEqual(await self.client.search("retry", 5), "ok")
        self.assertEqual(len(self.calls), 2)
        for status in [401, 403, 429, 500]:
            self.statuses = [status]
            before = len(self.calls)
            with self.assertRaises(SearchError):
                await self.client.search(str(status), 5)
            self.assertEqual(len(self.calls), before + 1)
            self.assertEqual(await self.client.search(str(status), 5), "ok")

    async def test_bad_rpc_sse_response_limit_timeout_and_close(self):
        self.raw = '{"result":{"isError":true,"content":[{"type":"text","text":"secret"}]}}'
        with self.assertRaises(SearchError) as caught:
            await self.client.search("bad", 5)
        self.assertNotIn("secret", str(caught.exception))
        self.raw = "event: message\ndata: " + json.dumps(self.payload) + "\n\n"
        self.assertEqual(await self.client.search("sse", 5), "ok")
        self.raw = "x" * 2000
        self.client.response_limit = 1000
        with self.assertRaises(SearchError):
            await self.client.search("large", 5)
        self.raw = None
        self.client.timeout = 0.01
        await self.client._session.close()
        self.delay = 0.1
        with self.assertRaises(SearchError) as caught:
            await self.client.search("timeout", 5)
        self.assertIn("超时", str(caught.exception))
        await self.client.close()
        self.assertTrue(self.client._session.closed)
        self.assertEqual(self.client._cache, {})
        with self.assertRaises(SearchError):
            await self.client.search("closed", 5)


if __name__ == "__main__":
    unittest.main()
