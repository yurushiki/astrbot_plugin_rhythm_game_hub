from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

import time
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional
from decimal import Decimal, ROUND_DOWN

MACHINES = ("mai", "chu", "ong")

# ======================
# 八折后的价格
# ======================
RATES = {
    "mai": Decimal("0.08"),   # 原 0.10
    "chu": Decimal("0.12"),   # 原 0.15
    "ong": Decimal("0.12"),   # 原 0.15
}

DAILY_CAP = Decimal("50.00")  # 每自然日封顶 50 元


# ======================
# 工具函数
# ======================

def fmt_user(nickname: str, uid: str) -> str:
    return f"{nickname}（{uid}）"


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%y/%m/%d %H:%M:%S")


def fmt_hms_cn(seconds: float) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}小时{m}分钟{s}秒"


def fmt_hms_colon(seconds: float) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def money_str(amount: Decimal) -> str:
    amount = amount.quantize(Decimal("0.00"), rounding=ROUND_DOWN)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, "f")


def parse_machine_arg(message_str: str, command: str) -> Optional[str]:
    t = (message_str or "").strip()
    if not t:
        return None
    parts = t.split()
    if parts and parts[0].lstrip("/") == command:
        parts = parts[1:]
    if not parts:
        return None
    m = parts[0].strip().lower()
    return m if m in MACHINES else None


def date_key_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def start_of_next_day_ts(ts: float) -> float:
    dt = datetime.fromtimestamp(ts)
    next_day = dt.date() + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day).timestamp()


# ======================
# 数据结构
# ======================

class UserRuntime:
    def __init__(self, uid: str, nickname: str):
        self.uid = uid
        self.nickname = nickname
        self.daily_totals: Dict[str, Dict[str, float]] = {}
        self.first_on_ts: Optional[float] = None
        self.active_machine: Optional[str] = None
        self.start_ts: Optional[float] = None


class GroupState:
    def __init__(self):
        self.users: Dict[str, UserRuntime] = {}


# ======================
# 核心业务
# ======================

class ArcadeManager:
    def __init__(self):
        self.groups: Dict[str, GroupState] = {}

    def _g(self, gid: str) -> GroupState:
        if gid not in self.groups:
            self.groups[gid] = GroupState()
        return self.groups[gid]

    def _u(self, g: GroupState, uid: str, nickname: str) -> UserRuntime:
        if uid not in g.users:
            g.users[uid] = UserRuntime(uid, nickname)
        else:
            g.users[uid].nickname = nickname
        return g.users[uid]

    def _add_interval(self, u: UserRuntime, machine: str, start_ts: float, end_ts: float) -> None:
        cur = start_ts
        while cur < end_ts:
            boundary = start_of_next_day_ts(cur)
            seg_end = min(end_ts, boundary)
            dkey = date_key_from_ts(cur)
            if dkey not in u.daily_totals:
                u.daily_totals[dkey] = {m: 0.0 for m in MACHINES}
            u.daily_totals[dkey][machine] += seg_end - cur
            cur = seg_end

    def _freeze_if_active(self, u: UserRuntime, now: float) -> None:
        if u.active_machine and u.start_ts:
            self._add_interval(u, u.active_machine, u.start_ts, now)
        u.active_machine = None
        u.start_ts = None

    def _calc_amount_with_daily_cap(self, daily: Dict[str, Dict[str, float]]) -> Decimal:
        total = Decimal("0.00")
        for mobj in daily.values():
            day_amount = Decimal("0.00")
            for m in MACHINES:
                minutes = int(mobj.get(m, 0.0) // 60)
                if minutes > 0:
                    day_amount += Decimal(minutes) * RATES[m]
            if day_amount > DAILY_CAP:
                day_amount = DAILY_CAP
            total += day_amount.quantize(Decimal("0.00"), rounding=ROUND_DOWN)
        return total.quantize(Decimal("0.00"), rounding=ROUND_DOWN)

    def on_machine(self, gid: str, uid: str, nickname: str, machine_id: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)
        now = time.time()

        if u.first_on_ts is None:
            u.first_on_ts = now

        if u.active_machine and u.active_machine != machine_id:
            self._freeze_if_active(u, now)

        u.active_machine = machine_id
        u.start_ts = now

        return (
            f"用户{fmt_user(nickname, uid)}\n"
            f"{machine_id}上机计费开始\n"
            f"当前时间为{fmt_time(now)}"
        )

    def pause(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)
        now = time.time()
        self._freeze_if_active(u, now)
        return f"用户{fmt_user(nickname, uid)}已暂停\n当前时间为{fmt_time(now)}"

    def timing(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)
        now = time.time()

        sec_map = {m: 0.0 for m in MACHINES}
        for mobj in u.daily_totals.values():
            for m in MACHINES:
                sec_map[m] += mobj.get(m, 0.0)
        if u.active_machine and u.start_ts:
            sec_map[u.active_machine] += now - u.start_ts

        lines = [
            f"用户{fmt_user(nickname, uid)}",
            f"当前时间为{fmt_time(now)}",
            "您的总上机时长为：",
        ]
        for m in MACHINES:
            lines.append(f"{m}：{fmt_hms_colon(sec_map[m])}")

        amount = self._calc_amount_with_daily_cap(u.daily_totals)
        lines.append(f"总金额为：{money_str(amount)}元")
        return "\n".join(lines)

    def off_machine(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)
        now = time.time()

        if u.first_on_ts is None:
            return "您当前未上机"

        self._freeze_if_active(u, now)

        total_seconds = sum(
            mobj.get(m, 0.0) for mobj in u.daily_totals.values() for m in MACHINES
        )
        amount = self._calc_amount_with_daily_cap(u.daily_totals)

        first_on = u.first_on_ts

        u.daily_totals = {}
        u.first_on_ts = None
        u.active_machine = None
        u.start_ts = None

        return (
            f"用户{fmt_user(nickname, uid)}\n"
            f"您已下机！\n"
            f"您的上机时间为：\n"
            f"{fmt_time(first_on)}\n"
            f"游玩时长为：\n"
            f"{fmt_hms_cn(total_seconds)}\n"
            f"金额：\n"
            f"{money_str(amount)}元\n"
            f"结账后请截图并发在本群内，感谢支持！"
        )

    def wojis(self, gid: str) -> str:
        g = self._g(gid)
        users = list(g.users.values())

        lines = [
            f"当前在线人数：{len(users)}",
            "在店人数有：",
        ]

        users.sort(key=lambda u: u.first_on_ts or 1e18)
        for u in users:
            lines.append(f"用户{fmt_user(u.nickname, u.uid)}")
            if u.first_on_ts:
                lines.append(f"上机时间：{fmt_time(u.first_on_ts)}")
            else:
                lines.append("上机时间：未上机")
            lines.append("---")

        return "\n".join(lines)


# ======================
# 插件入口
# ======================

@register("arcade", "YourName", "音游窝管理插件（八折计费）", "1.11.0")
class ArcadePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.mgr = ArcadeManager()
        self._lock = asyncio.Lock()
        self._state_path = os.path.join(os.path.dirname(__file__), "arcade_state.json")

    async def initialize(self):
        if os.path.exists(self._state_path):
            with open(self._state_path, "r", encoding="utf-8") as f:
                self.mgr.groups = json.load(f)
        logger.info("ArcadePlugin loaded")

    async def terminate(self):
        with open(self._state_path, "w", encoding="utf-8") as f:
            json.dump(self.mgr.groups, f, ensure_ascii=False)
        logger.info("ArcadePlugin unloaded")

    @filter.command("上机")
    async def cmd_on(self, event: AstrMessageEvent):
        m = parse_machine_arg(event.message_str, "上机")
        if not m:
            yield event.plain_result("请输入机器：mai / chu / ong")
            return
        yield event.plain_result(
            self.mgr.on_machine(
                str(event.get_group_id()),
                str(event.get_sender_id()),
                event.get_sender_name(),
                m,
            )
        )

    @filter.command("暂停")
    async def cmd_pause(self, event: AstrMessageEvent):
        yield event.plain_result(
            self.mgr.pause(
                str(event.get_group_id()),
                str(event.get_sender_id()),
                event.get_sender_name(),
            )
        )

    @filter.command("计时")
    async def cmd_timing(self, event: AstrMessageEvent):
        yield event.plain_result(
            self.mgr.timing(
                str(event.get_group_id()),
                str(event.get_sender_id()),
                event.get_sender_name(),
            )
        )

    @filter.command("下机")
    async def cmd_off(self, event: AstrMessageEvent):
        yield event.plain_result(
            self.mgr.off_machine(
                str(event.get_group_id()),
                str(event.get_sender_id()),
                event.get_sender_name(),
            )
        )

    @filter.command("窝几")
    async def cmd_wojis(self, event: AstrMessageEvent):
        yield event.plain_result(self.mgr.wojis(str(event.get_group_id())))
