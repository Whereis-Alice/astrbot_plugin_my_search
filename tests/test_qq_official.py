"""QQ replies use real AstrBot events and botpy's public API with local HTTP mocks."""

import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from botpy.api import BotAPI
from botpy.errors import ServerError
from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message
from test_plugin import LocalContext, LocalEvent, MySearchPlugin, SearchError, package

formatting = importlib.import_module(package.__name__ + ".qq_formatting")
sender = importlib.import_module(package.__name__ + ".qq_official")


class QQFormattingTests(unittest.TestCase):
    def test_supported_headings_and_intact_encoded_source_links(self):
        pages, truncated = formatting.build_qq_pages(
            "## Search Results (2 results)\n### 1. 页面标题\n"
            "- **URL**: https://example.com/wiki/A_(B)?q=中文&x=1\n"
            "[另一来源](https://other.example/path_(part))\n"
            "![插图](https://image.example/x.png)\n正文。",
        )
        self.assertFalse(truncated)
        self.assertEqual(len(pages), 1)
        md = pages[0].markdown
        self.assertIn("## 我的搜索", md)
        self.assertIn("## 1. 页面标题", md)
        self.assertNotIn("###", md)
        self.assertNotIn("Search Results", md)
        self.assertIn("[查看来源](https://example.com/wiki/A_%28B%29?q=%E4%B8%AD%E6%96%87&x=1)", md)
        self.assertIn("[另一来源](https://other.example/path_%28part%29)", md)
        self.assertIn("[图片：插图]", md)
        self.assertNotIn("![", md)
        self.assertIn("来源：", pages[0].plain)

    def test_hidden_and_unsupported_links_are_not_clickable(self):
        text = "[https://example.com](https://example.com) <https://other.example>\n"
        for hidden in (True, False):
            pages, _ = formatting.build_qq_pages(text, show_links=not hidden)
            page = pages[0] if hidden else formatting.hide_page_links(pages[0])
            for body in (page.markdown, page.plain):
                self.assertNotIn("https://", body)
                self.assertNotIn("example.com", body)
                self.assertIn("链接已隐藏", body)
        pages, _ = formatting.build_qq_pages("[不可用](javascript:alert(1)) http://127.0.0.1/a")
        self.assertNotIn("javascript:", pages[0].markdown)
        self.assertNotIn("127.0.0.1", pages[0].markdown)
        pages, _ = formatting.build_qq_pages(
            "[不完整](https://example.com/private", show_links=False
        )
        self.assertNotIn("https://", pages[0].markdown)

    def test_limits_truncate_both_formats_without_cutting_a_link(self):
        text = "x" * 190 + " [来源](https://example.com/完整路径)\n" + "内容" * 3000
        pages, truncated = formatting.build_qq_pages(
            text,
            chunk_size=300,
            max_messages=3,
            output_limit=500,
        )
        self.assertTrue(truncated)
        self.assertLessEqual(len(pages), 3)
        self.assertIn("已截断", pages[-1].markdown)
        self.assertLessEqual(sum(len(page.markdown) for page in pages), 500)
        for page in pages:
            self.assertLessEqual(len(page.markdown), 300)
            self.assertLessEqual(len(page.plain), 300)
            if "https://" in page.markdown:
                self.assertIn(
                    "[来源](https://example.com/%E5%AE%8C%E6%95%B4%E8%B7%AF%E5%BE%84)",
                    page.markdown,
                )
        pages, _ = formatting.build_qq_pages("https://example.com/" + "a" * 600, chunk_size=300)
        self.assertIn("链接过长", pages[0].markdown)
        self.assertNotIn("https://", pages[0].markdown)


class QQDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = MySearchPlugin(LocalContext(), {})
        self.request = AsyncMock(return_value={"id": "sent-id"})
        self.api = BotAPI(SimpleNamespace(request=self.request))
        self.bookkeeping_patch = patch.object(AstrMessageEvent, "send", new_callable=AsyncMock)
        self.bookkeeping = self.bookkeeping_patch.start()
        self.addCleanup(self.bookkeeping_patch.stop)

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def event(self, source_type=GroupMessage, platform="qq_official"):
        local = LocalEvent(platform=platform)
        raw = source_type(
            self.api,
            None,
            {
                "id": "received-id",
                "content": "搜一搜 test",
                "group_openid": "group-openid",
                "channel_id": "channel-id",
                "guild_id": "guild-id",
                "author": {"member_openid": "member-openid", "user_openid": "user-openid"},
            },
        )
        local.message_obj.raw_message = raw
        local.message_obj.message_id = "received-id"
        return QQOfficialMessageEvent(
            "搜一搜 test",
            local.message_obj,
            local.platform_meta,
            "local-session",
            SimpleNamespace(api=self.api),
        )

    async def test_real_group_c2c_channel_and_dm_reply_routes(self):
        for source_type, route, v2 in (
            (GroupMessage, "/v2/groups/group-openid/messages", True),
            (C2CMessage, "/v2/users/user-openid/messages", True),
            (Message, "/channels/channel-id/messages", False),
            (DirectMessage, "/dms/guild-id/messages", False),
        ):
            with self.subTest(source=source_type.__name__):
                self.request.reset_mock()
                await self.plugin._send_result(
                    self.event(source_type), "来源：https://example.com/a"
                )
                self.assertEqual(self.request.await_count, 1)
                args, kwargs = self.request.call_args
                self.assertTrue(args[0].url.endswith(route))
                payload = kwargs["json"]
                self.assertEqual(payload["msg_id"], "received-id")
                self.assertIn("[查看来源](https://example.com/a)", payload["markdown"]["content"])
                self.assertFalse(payload.get("content"))
                if v2:
                    self.assertEqual(payload["msg_type"], 2)
                    self.assertEqual(payload["msg_seq"], 1)
                else:
                    self.assertNotIn("msg_type", payload)

    async def test_webhook_counts_sequences_and_private_reply_cap(self):
        self.plugin.qq_max_messages = 5
        for source_type, platform, expected in (
            (GroupMessage, "qq_official_webhook", 5),
            (C2CMessage, "qq_official", 4),
        ):
            self.request.reset_mock()
            await self.plugin._send_result(self.event(source_type, platform), "长正文" * 6000)
            self.assertEqual(self.request.await_count, expected)
            payloads = [call.kwargs["json"] for call in self.request.call_args_list]
            self.assertEqual(
                [payload["msg_seq"] for payload in payloads], list(range(1, expected + 1))
            )
            self.assertIn("已截断", payloads[-1]["markdown"]["content"])
            self.assertTrue(
                all(
                    len(payload["markdown"]["content"]) <= self.plugin.chunk_size
                    for payload in payloads
                )
            )

    async def test_markdown_rejection_falls_back_once_and_keeps_sent_pages(self):
        self.request.side_effect = [
            {"id": "first-id"},
            ServerError("不允许发送原生 markdown private-data"),
            {"id": "second-id"},
            {"id": "third-id"},
        ]
        with patch.object(sender.logger, "info") as info:
            await self.plugin._send_result(self.event(), "body" * 900)
        payloads = [call.kwargs["json"] for call in self.request.call_args_list]
        self.assertEqual([p["msg_seq"] for p in payloads], [1, 2, 2, 3])
        self.assertEqual([p["msg_type"] for p in payloads], [2, 2, 0, 0])
        self.assertEqual(self.bookkeeping.await_count, 3)
        self.assertEqual(info.call_args.args[2:4], (1, 2))
        self.assertNotIn("private-data", str(info.call_args))

    async def test_url_rejection_hides_only_links_and_keeps_markdown(self):
        self.request.side_effect = [
            ServerError("不允许发送 URL"),
            {"id": "first"},
            {"id": "second"},
        ]
        text = "[来源](https://example.com/private)\n" + "内容" * 600 + " https://other.example/a"
        await self.plugin._send_result(self.event(), text)
        payloads = [call.kwargs["json"] for call in self.request.call_args_list]
        self.assertEqual([p["msg_type"] for p in payloads], [2, 2, 2])
        self.assertEqual([p["msg_seq"] for p in payloads], [1, 1, 2])
        self.assertIn("https://", payloads[0]["markdown"]["content"])
        self.assertTrue(all("https://" not in p["markdown"]["content"] for p in payloads[1:]))
        self.assertIn("链接已隐藏", payloads[1]["markdown"]["content"])

    async def test_combined_rejections_do_not_resend_successful_parts(self):
        self.request.side_effect = [
            ServerError("40034127 无Markdown模板权限"),
            ServerError("40054010 不允许发送 URL"),
            {"id": "sent"},
        ]
        await self.plugin._send_result(self.event(), "https://example.com/private")
        payloads = [call.kwargs["json"] for call in self.request.call_args_list]
        self.assertEqual([p["msg_type"] for p in payloads], [2, 0, 0])
        self.assertNotIn("https://", payloads[-1]["content"])
        self.assertEqual(self.bookkeeping.await_count, 1)

    async def test_uncertain_errors_and_empty_responses_are_never_success_or_retried(self):
        for failure in (
            asyncio.TimeoutError("private-payload"),
            ServerError("40034006 消息内容违规 private-payload"),
            None,
            {},
        ):
            self.request.reset_mock()
            self.request.side_effect = failure if isinstance(failure, Exception) else None
            self.request.return_value = failure
            self.bookkeeping.reset_mock()
            with (
                patch.object(sender.logger, "info") as info,
                patch.object(sender.logger, "error") as error,
            ):
                with self.assertRaises(SearchError) as caught:
                    await self.plugin._send_result(self.event(), "body")
            self.assertEqual(self.request.await_count, 1)
            self.bookkeeping.assert_not_awaited()
            info.assert_not_called()
            self.assertNotIn("private-payload", str(error.call_args))
            self.assertNotIn("private-payload", str(caught.exception))

    async def test_disabled_markdown_hidden_links_and_command_integration(self):
        self.plugin.qq_markdown = False
        self.plugin.qq_show_links = False
        self.plugin.client.extract = AsyncMock(return_value="正文 https://example.com/page")
        event = self.event(C2CMessage)
        self.assertEqual(
            [
                result
                async for result in self.plugin.my_extract_command(
                    event, "https://example.com/page"
                )
            ],
            [],
        )
        payload = self.request.call_args.kwargs["json"]
        self.assertEqual(payload["msg_type"], 0)
        self.assertFalse(payload.get("markdown"))
        self.assertNotIn("https://", payload["content"])
        self.assertIn("网页正文", payload["content"])
        self.assertFalse(event.call_llm)
        configured = MySearchPlugin(
            LocalContext(), {"qq_official_max_messages": 99, "qq_official_markdown": "false"}
        )
        self.assertEqual(configured.qq_max_messages, 5)
        self.assertFalse(configured.qq_markdown)
        await configured.terminate()

    async def test_invalid_message_id_or_openid_never_sends_active_messages(self):
        for field in ("id", "group_openid"):
            event = self.event()
            setattr(event.message_obj.raw_message, field, None)
            with self.assertRaises(SearchError):
                await self.plugin._send_result(event, "body")
        self.request.assert_not_awaited()
