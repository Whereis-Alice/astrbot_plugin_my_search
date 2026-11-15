"""Reply through AstrBot's existing botpy client, including older 4.x SDKs."""

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from .client import SearchError
from .qq_formatting import build_qq_pages, hide_page_links

QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
_MD_CODES = {304036, 40034011, 40034124, 40034127}


def _rejection(exc: Exception) -> str | None:
    # botpy sometimes keeps only the message, dropping the numeric error code.
    from botpy.errors import ForbiddenError, MethodNotAllowedError, ServerError

    if not isinstance(exc, (ForbiddenError, MethodNotAllowedError, ServerError)):
        return None
    text = re.sub(r"\s+", "", str(exc)).lower()
    if "不允许发送url" in text or "urlnotallowed" in text or "40054010" in text:
        return "url"
    if any(str(code) in text for code in _MD_CODES) or any(
        phrase in text
        for phrase in (
            "不允许发送原生markdown",
            "无markdown模板权限",
            "无效的markdown内容",
            "markdown消息参数错误",
        )
    ):
        return "markdown"
    return None


async def send_qq_result(
    event: AstrMessageEvent,
    text: str,
    *,
    markdown: bool,
    show_links: bool,
    max_messages: int,
    chunk_size: int,
    output_limit: int,
    title: str = "搜索结果",
) -> None:
    # QQ dependencies already belong to AstrBot; other platforms need not import them.
    from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message

    source = event.message_obj.raw_message
    if not isinstance(source, (GroupMessage, C2CMessage, Message, DirectMessage)):
        raise SearchError(
            "QQ 官 bot 消息来源不可用，请检查 AstrBot 平台适配器。",
            code="qq_message_source_invalid",
        )
    v2 = isinstance(source, (GroupMessage, C2CMessage))
    max_messages = min(max_messages, 4 if isinstance(source, C2CMessage) else 5)
    if not source.id:
        raise SearchError(
            "QQ 官 bot 缺少被回复的消息 ID，请检查平台适配器。", code="qq_message_source_invalid"
        )
    if (isinstance(source, GroupMessage) and not source.group_openid) or (
        isinstance(source, C2CMessage) and not source.author.user_openid
    ):
        raise SearchError(
            "QQ 官 bot 缺少会话 OpenID，请检查平台适配器。", code="qq_message_source_invalid"
        )
    pages, truncated = build_qq_pages(
        text,
        title=title,
        show_links=show_links,
        chunk_size=chunk_size,
        max_messages=max_messages,
        output_limit=output_limit,
    )
    sent = md_sent = 0
    for page in pages:
        seq = event.get_extra("my_search_qq_msg_seq", 0) + 1
        for attempt in range(3):
            current = page if show_links else hide_page_links(page)
            body = current.markdown if markdown else current.plain
            payload = {"markdown": {"content": body}} if markdown else {"content": body}
            if v2:
                payload.update(msg_type=2 if markdown else 0, msg_seq=seq)
            try:
                # Public SDK reply() keeps the original msg_id and reuses AstrBot auth.
                response = await source.reply(**payload)
                message_id = (
                    response.get("id")
                    if isinstance(response, dict)
                    else getattr(response, "id", None)
                )
                if not message_id:
                    raise RuntimeError("QQ send API returned no message ID")
            except Exception as exc:  # noqa: BLE001 - QQ SDK wraps platform errors.
                rejection = _rejection(exc)
                if rejection == "markdown" and markdown and attempt < 2:
                    markdown = False
                    logger.warning("我的搜索：QQ 官 bot 拒绝 Markdown，当前及后续消息改用文本。")
                    continue
                if rejection == "url" and show_links and attempt < 2:
                    show_links = False
                    logger.warning("我的搜索：QQ 官 bot 拒绝 URL，隐藏当前及后续消息的链接。")
                    continue
                logger.error(
                    "我的搜索：QQ 官 bot 发送失败，平台=%s，已发送=%d/%d条，异常类型=%s。",
                    event.get_platform_name(),
                    sent,
                    len(pages),
                    type(exc).__name__,
                )
                raise SearchError(
                    f"QQ 官 bot 消息发送失败（已发送 {sent}/{len(pages)} 条）。请检查机器人权限、回复额度和平台日志。",
                    code="qq_message_send_failed",
                ) from None
            # Record the successful operation without asking the adapter to send again.
            chain = MessageChain([Plain(body)]).use_t2i(False)
            if hasattr(chain, "use_markdown"):
                chain.use_markdown(markdown)
            await AstrMessageEvent.send(event, chain)
            event.set_extra("my_search_qq_msg_seq", seq)
            sent += 1
            md_sent += int(markdown)
            break
    logger.info(
        "我的搜索：QQ 官 bot 发送成功，平台=%s，Markdown=%d条，文本=%d条，链接=%s，已截断=%s。",
        event.get_platform_name(),
        md_sent,
        sent - md_sent,
        "显示" if show_links else "隐藏",
        "是" if truncated else "否",
    )
