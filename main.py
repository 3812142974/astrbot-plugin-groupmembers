import asyncio
import base64
import random
import re

import httpx

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At, Image, Node, Nodes, Plain
from astrbot.api import logger

# QQ 头像规格：40 / 100 / 140 / 640。群人数很多时建议调小（如 100），
# 否则合并转发的体积会非常大，可能导致发送缓慢或被 OneBot 端拒绝。
AVATAR_SPEC = 640
# 同时下载头像的最大并发数（避免一次性打爆网络）。
AVATAR_CONCURRENCY = 8


@register(
    "astrbot_plugin_groupmembers",
    "3812142974",
    "将群成员（全部 / 指定@ / 随机N人）的头像、昵称、QQ 号整合成一条合并转发消息发出。",
    "1.0.0",
)
class GroupMembersCard(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ---------------- 工具方法 ----------------

    async def _download_avatar(self, qq: str) -> str | None:
        """下载指定 QQ 的头像并转为 base64，失败返回 None（该成员仍会被收录，只是没有头像）。"""
        url = f"https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec={AVATAR_SPEC}"
        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
                if not data or len(data) < 100:
                    return None
                return "base64://" + base64.b64encode(data).decode()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"下载 QQ {qq} 头像失败: {e}")
            return None

    @staticmethod
    def _parse_args(event: AstrMessageEvent):
        """从消息中解析出 (被@的QQ列表, 数字参数)。"""
        mentioned = []
        for seg in event.get_messages():
            if isinstance(seg, At) and str(seg.qq) != "all":
                mentioned.append(str(seg.qq))
        text = event.message_str or ""
        m = re.search(r"(\d+)", text)
        number = int(m.group(1)) if m else None
        return mentioned, number

    async def _make_node(self, sem: asyncio.Semaphore, qq: str, name: str):
        async with sem:
            b64 = await self._download_avatar(qq)
            content = []
            if b64:
                content.append(Image(file=b64))
            content.append(Plain(f"QQ：{qq}\n昵称：{name}"))
            return Node(content=content, uin=qq, name=name)

    async def _build(self, event: AstrMessageEvent, require_mention: bool):
        if not event.get_group_id():
            return event.plain_result("请在群聊中使用该指令～")

        group = await event.get_group()
        if not group or not group.members:
            return event.plain_result(
                "获取群成员列表失败，请确认机器人具备获取群成员信息的权限。"
            )

        all_members = group.members
        by_id = {str(m.user_id): m for m in all_members}

        mentioned, number = self._parse_args(event)

        if require_mention:
            if not mentioned:
                return event.plain_result(
                    "请 @ 需要收录的成员，例如：/群成员at @小明 @小红"
                )
            candidates = [by_id[q] for q in mentioned if q in by_id]
            if not candidates:
                return event.plain_result("在群成员列表中未找到被 @ 的成员。")
        else:
            # 主指令：默认全部；若带了 @ 则只收录被 @ 的人。
            candidates = (
                [by_id[q] for q in mentioned if q in by_id]
                if mentioned
                else list(all_members)
            )

        if number:
            number = max(1, min(number, len(candidates)))
            candidates = random.sample(candidates, number)

        sem = asyncio.Semaphore(AVATAR_CONCURRENCY)
        nodes = await asyncio.gather(
            *(
                self._make_node(sem, str(m.user_id), m.nickname or str(m.user_id))
                for m in candidates
            )
        )
        return event.chain_result([Nodes(nodes=nodes)])

    # ---------------- 指令 ----------------

    @filter.command("群成员")
    async def cmd_group_members(self, event: AstrMessageEvent):
        """把整个群（或随机 N 人 / 指定@的人）的头像、昵称、QQ 整合成一条合并转发发出"""
        yield event.plain_result("正在生成群成员合并转发，请稍候…")
        result = await self._build(event, require_mention=False)
        yield result

    @filter.command("群成员at")
    async def cmd_group_members_at(self, event: AstrMessageEvent):
        """@ 谁就只收录谁：/群成员at @A @B（可加数字表示随机几个人）"""
        yield event.plain_result("正在生成指定成员合并转发，请稍候…")
        result = await self._build(event, require_mention=True)
        yield result
