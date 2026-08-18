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
# 单条合并转发允许的最大节点数（OneBot 限制，超过会发送失败）。
MAX_NODES_PER_FORWARD = 100


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

    @staticmethod
    def _valid_member(m, self_id: str) -> bool:
        """过滤掉机器人自身（get_group_member_list 也会返回 bot，且 user_id 常为 0）
        以及无效 id（0 / all / 空）。"""
        uid = str(m.user_id)
        if not uid or uid in ("0", "all"):
            return False
        if self_id and uid == str(self_id):
            return False
        return True

    async def _build(self, event: AstrMessageEvent, require_mention: bool):
        if not event.get_group_id():
            yield event.plain_result("请在群聊中使用该指令～")
            return

        group = await event.get_group()
        if not group or not group.members:
            yield event.plain_result(
                "获取群成员列表失败，请确认机器人具备获取群成员信息的权限。"
            )
            return

        self_id = event.get_self_id()
        all_members = [m for m in group.members if self._valid_member(m, self_id)]
        by_id = {str(m.user_id): m for m in all_members}

        mentioned, number = self._parse_args(event)

        if require_mention:
            if not mentioned:
                yield event.plain_result(
                    "请 @ 需要收录的成员，例如：/群成员头像at @小明 @小红"
                )
                return
            candidates = [by_id[q] for q in mentioned if q in by_id]
            if not candidates:
                yield event.plain_result("在群成员列表中未找到被 @ 的成员。")
                return
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

        total = len(nodes)
        if total == 0:
            yield event.plain_result("没有可收录的成员。")
            return

        # 合并转发单条节点上限为 100，超过则拆成多条发送。
        chunks = [
            nodes[i : i + MAX_NODES_PER_FORWARD]
            for i in range(0, total, MAX_NODES_PER_FORWARD)
        ]
        if len(chunks) > 1:
            yield event.plain_result(
                f"共 {total} 人，超过单条转发上限，分 {len(chunks)} 条合并转发发送～"
            )
        for idx, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                yield event.plain_result(f"第 {idx}/{len(chunks)} 条")
            yield event.chain_result([Nodes(nodes=chunk)])

    # ---------------- 指令 ----------------

    @filter.command("群成员头像")
    async def cmd_group_members(self, event: AstrMessageEvent):
        """把整个群（或随机 N 人 / 指定@的人）的头像、昵称、QQ 整合成合并转发发出"""
        yield event.plain_result("正在生成群成员合并转发，请稍候…")
        async for result in self._build(event, require_mention=False):
            yield result

    @filter.command("群成员头像at")
    async def cmd_group_members_at(self, event: AstrMessageEvent):
        """@ 谁就只收录谁：/群成员头像at @A @B（可加数字表示随机几个人）"""
        yield event.plain_result("正在生成指定成员合并转发，请稍候…")
        async for result in self._build(event, require_mention=True):
            yield result
