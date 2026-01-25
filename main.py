from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

import time
import os
import json
import asyncio
from datetime import datetime, date, timedelta
from typing import Dict, Optional
from decimal import Decimal, ROUND_DOWN

MACHINES = ("mai", "chu", "ong")

RATES = {
    "mai": Decimal("0.10"),
    "chu": Decimal("0.15"),
    "ong": Decimal("0.15"),
}

DAILY_CAP = Decimal("50.00")  # 每自然日封顶 50 元


# ======================
# 工具函数
# ======================

def fmt_user(nickname: str, uid: str) -> str:
    return f"{nickname}（{uid}）"


def fmt_time(ts: float) -> str:
    # 年两位 + HH:MM:SS（不带中文单位）
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
    # 用本地时区日期作为自然日 key
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def start_of_next_day_ts(ts: float) -> float:
    dt = datetime.fromtimestamp(ts)
    next_day = (dt.date() + timedelta(days=1))
    return datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0).timestamp()


# ======================
# 数据结构（未结算账单，按天累加）
# ======================

class UserRuntime:
    """
    未结算账单（从第一次上机开始，到 /下机 结账清零）：
    - daily_totals[YYYY-MM-DD][machine] = seconds
    - first_on_ts: 本次第一次 /上机 的时间（用于 /下机 显示）
    - active_machine/start_ts: 当前正在计时的机器段
    """
    def __init__(self, uid: str, nickname: str):
        self.uid = uid
        self.nickname = nickname

        self.daily_totals: Dict[str, Dict[str, float]] = {}  # date -> {machine: seconds}
        self.first_on_ts: Optional[float] = None

        self.active_machine: Optional[str] = None
        self.start_ts: Optional[float] = None


class GroupState:
    def __init__(self):
        self.in_store: Dict[str, dict] = {}          # uid -> {"nickname": str, "enter_ts": float}
        self.users: Dict[str, UserRuntime] = {}      # uid -> UserRuntime


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

    # ---------- 持久化 ----------
    def to_dict(self) -> dict:
        out = {}
        for gid, g in self.groups.items():
            out[gid] = {
                "in_store": g.in_store,
                "users": {
                    uid: {
                        "uid": u.uid,
                        "nickname": u.nickname,
                        "daily_totals": u.daily_totals,
                        "first_on_ts": u.first_on_ts,
                        "active_machine": u.active_machine,
                        "start_ts": u.start_ts,
                    }
                    for uid, u in g.users.items()
                }
            }
        return out

    def load_dict(self, data: dict) -> None:
        self.groups = {}
        if not isinstance(data, dict):
            return

        for gid, raw in data.items():
            gs = GroupState()

            store = (raw or {}).get("in_store", {}) or {}
            if isinstance(store, dict):
                for uid, v in store.items():
                    try:
                        nick = str((v or {}).get("nickname", ""))
                        enter_ts = float((v or {}).get("enter_ts", time.time()))
                        gs.in_store[str(uid)] = {"nickname": nick, "enter_ts": enter_ts}
                    except Exception:
                        continue

            users = (raw or {}).get("users", {}) or {}
            if isinstance(users, dict):
                for uid, uraw in users.items():
                    try:
                        u = UserRuntime(str(uid), str((uraw or {}).get("nickname", "")))

                        # 新结构：daily_totals
                        dtot = (uraw or {}).get("daily_totals", None)
                        if isinstance(dtot, dict):
                            for dkey, mobj in dtot.items():
                                if not isinstance(mobj, dict):
                                    continue
                                u.daily_totals[str(dkey)] = {}
                                for m in MACHINES:
                                    try:
                                        u.daily_totals[str(dkey)][m] = float(mobj.get(m, 0.0))
                                    except Exception:
                                        u.daily_totals[str(dkey)][m] = 0.0

                        # 兼容旧结构：visit_totals（若存在则归到 first_on_ts 所在日期或今天）
                        vt = (uraw or {}).get("visit_totals", None)
                        if isinstance(vt, dict) and not u.daily_totals:
                            fo = (uraw or {}).get("first_on_ts", None)
                            base_ts = float(fo) if fo is not None else time.time()
                            dkey = date_key_from_ts(base_ts)
                            u.daily_totals[dkey] = {m: float(vt.get(m, 0.0)) for m in MACHINES}

                        fo = (uraw or {}).get("first_on_ts", None)
                        u.first_on_ts = float(fo) if fo is not None else None

                        am = (uraw or {}).get("active_machine", None)
                        st = (uraw or {}).get("start_ts", None)
                        u.active_machine = str(am) if am in MACHINES else None
                        u.start_ts = float(st) if (st is not None and u.active_machine) else None

                        gs.users[str(uid)] = u
                    except Exception:
                        continue

            self.groups[str(gid)] = gs

    # ---------- 进店 / 离店 ----------
    def enter_store(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        g.in_store[uid] = {"nickname": nickname, "enter_ts": time.time()}
        self._u(g, uid, nickname)
        return f"用户{fmt_user(nickname, uid)}已进店。"

    def leave_store(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        # 离店只影响在店名单（/窝几），不触碰计费与计时状态
        self._u(g, uid, nickname)
        g.in_store.pop(uid, None)
        return f"用户{fmt_user(nickname, uid)}离开了"

    def ensure_in_store(self, gid: str, uid: str) -> bool:
        return uid in self._g(gid).in_store

    # ---------- 将某段时间按“自然日”切分并累加到 daily_totals ----------
    def _add_interval(self, u: UserRuntime, machine: str, start_ts: float, end_ts: float) -> None:
        if end_ts <= start_ts:
            return
        cur = start_ts
        while cur < end_ts:
            boundary = start_of_next_day_ts(cur)
            seg_end = min(end_ts, boundary)
            dkey = date_key_from_ts(cur)
            if dkey not in u.daily_totals:
                u.daily_totals[dkey] = {m: 0.0 for m in MACHINES}
            u.daily_totals[dkey][machine] = float(u.daily_totals[dkey].get(machine, 0.0)) + float(seg_end - cur)
            cur = seg_end

    # ---------- 冻结当前段（写入 daily_totals） ----------
    def _freeze_if_active(self, u: UserRuntime, now: Optional[float] = None) -> None:
        if u.active_machine and u.start_ts:
            tnow = time.time() if now is None else now
            self._add_interval(u, u.active_machine, u.start_ts, tnow)
        u.active_machine = None
        u.start_ts = None

    # ---------- 计算“包含当前段”的临时 daily_totals（不落盘） ----------
    def _temp_daily_totals_with_active(self, u: UserRuntime, now: float) -> Dict[str, Dict[str, float]]:
        temp: Dict[str, Dict[str, float]] = {}
        for dkey, mobj in u.daily_totals.items():
            temp[dkey] = {m: float(mobj.get(m, 0.0)) for m in MACHINES}

        if u.active_machine and u.start_ts and now > u.start_ts:
            cur = u.start_ts
            while cur < now:
                boundary = start_of_next_day_ts(cur)
                seg_end = min(now, boundary)
                dkey = date_key_from_ts(cur)
                if dkey not in temp:
                    temp[dkey] = {m: 0.0 for m in MACHINES}
                temp[dkey][u.active_machine] += float(seg_end - cur)
                cur = seg_end

        return temp

    # ---------- 计算总秒数（按机器汇总，包含当前段） ----------
    def _total_seconds_by_machine(self, u: UserRuntime, now: float) -> Dict[str, float]:
        out = {m: 0.0 for m in MACHINES}
        for dkey, mobj in u.daily_totals.items():
            for m in MACHINES:
                out[m] += float(mobj.get(m, 0.0))
        if u.active_machine and u.start_ts and now > u.start_ts:
            out[u.active_machine] += float(now - u.start_ts)
        return out

    # ---------- 计算金额：按天封顶 50 后汇总 ----------
    def _calc_amount_with_daily_cap(self, daily: Dict[str, Dict[str, float]]) -> Decimal:
        total = Decimal("0.00")
        for dkey, mobj in daily.items():
            day_amount = Decimal("0.00")
            for m in MACHINES:
                seconds = float(mobj.get(m, 0.0))
                minutes = int(seconds // 60)  # 未满一分钟舍掉
                if minutes <= 0:
                    continue
                day_amount += Decimal(minutes) * RATES[m]
            day_amount = day_amount.quantize(Decimal("0.00"), rounding=ROUND_DOWN)
            if day_amount > DAILY_CAP:
                day_amount = DAILY_CAP
            total += day_amount
        return total.quantize(Decimal("0.00"), rounding=ROUND_DOWN)

    # ---------- 上机（支持切换冻结；记录第一次上机时间） ----------
    def on_machine(self, gid: str, uid: str, nickname: str, machine_id: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)

        now = time.time()
        if u.first_on_ts is None:
            u.first_on_ts = now

        if u.active_machine:
            if u.active_machine != machine_id:
                self._freeze_if_active(u, now=now)
                u.active_machine = machine_id
                u.start_ts = now
        else:
            u.active_machine = machine_id
            u.start_ts = now

        return (
            f"用户{fmt_user(nickname, uid)}\n"
            f"{machine_id}上机计费开始\n"
            f"当前时间为{fmt_time(now)}"
        )

    # ---------- 暂停（冻结当前段；不清零；时间冒号格式） ----------
    def pause(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)
        now = time.time()
        self._freeze_if_active(u, now=now)
        return f"用户{fmt_user(nickname, uid)}已暂停\n当前时间为{fmt_time(now)}"

    # ---------- 计时 ----------
    def timing(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)

        now = time.time()
        sec_map = self._total_seconds_by_machine(u, now)
        played = [m for m in MACHINES if sec_map[m] > 0.0]

        lines = [
            f"用户{fmt_user(nickname, uid)}",
            f"当前时间为{fmt_time(now)}",
        ]

        if played:
            lines.append("您的总上机时长为：")
            for m in played:
                lines.append(f"{m}：{fmt_hms_colon(sec_map[m])}")

        # 金额：按天封顶 50 后汇总（包含当前段）
        temp_daily = self._temp_daily_totals_with_active(u, now)
        total_amount = self._calc_amount_with_daily_cap(temp_daily)
        lines.append(f"总金额为：{money_str(total_amount)}元")
        return "\n".join(lines)

    # ---------- 下机（结清未结算账单；上机时间取第一次上机时间） ----------
    def off_machine(self, gid: str, uid: str, nickname: str) -> str:
        g = self._g(gid)
        u = self._u(g, uid, nickname)

        now = time.time()
        sec_map = self._total_seconds_by_machine(u, now)
        total_seconds = sum(sec_map[m] for m in MACHINES)

        if u.first_on_ts is None or total_seconds <= 0.0:
            return "您当前未上机"

        # 先冻结当前段到 daily_totals
        self._freeze_if_active(u, now=now)

        # 金额：按天封顶 50 后汇总（已全部落入 daily_totals）
        total_amount = self._calc_amount_with_daily_cap(u.daily_totals)

        visit_total_seconds = 0.0
        for dkey, mobj in u.daily_totals.items():
            for m in MACHINES:
                visit_total_seconds += float(mobj.get(m, 0.0))

        first_on = u.first_on_ts

        # 结账清零未结算账单
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
            f"{fmt_hms_cn(visit_total_seconds)}\n"
            f"金额：\n"
            f"{money_str(total_amount)}元\n"
            f"结账后请截图并发在本群内，感谢支持！"
        )

    # ---------- 窝几（统计进店人数；上机时间冒号格式） ----------
    def wojis(self, gid: str) -> str:
        g = self._g(gid)

        lines = [
            f"当前在线人数：{len(g.in_store)}",
            "在店人数有：",
        ]

        sorted_items = sorted(g.in_store.items(), key=lambda kv: float((kv[1] or {}).get("enter_ts", 0.0)))

        for uid, info in sorted_items:
            nick = str((info or {}).get("nickname", ""))
            u = g.users.get(uid)

            lines.append(f"用户{fmt_user(nick, uid)}")

            if u and u.first_on_ts is not None:
                lines.append(f"上机时间：{fmt_time(u.first_on_ts)}")
            else:
                lines.append("上机时间：未上机")

            lines.append("---")

        return "\n".join(lines)


# ======================
# 插件入口（含落盘）
# ======================

@register("arcade", "YourName", "音游窝进店/离店/上机/暂停/计时/下机/窝几（三机，按天封顶）", "1.9.0")
class ArcadePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.mgr = ArcadeManager()
        self._lock = asyncio.Lock()
        self._state_path = os.path.join(os.path.dirname(__file__), "arcade_state.json")

    async def initialize(self):
        await self._load_state()
        logger.info("ArcadePlugin loaded")

    async def terminate(self):
        await self._save_state()
        logger.info("ArcadePlugin unloaded")

    async def _load_state(self):
        async with self._lock:
            if not os.path.exists(self._state_path):
                return
            try:
                with open(self._state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.mgr.load_dict(data)
            except Exception as e:
                logger.error(f"load_state failed: {e}")

    async def _save_state(self):
        async with self._lock:
            try:
                tmp = self._state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.mgr.to_dict(), f, ensure_ascii=False)
                os.replace(tmp, self._state_path)
            except Exception as e:
                logger.error(f"save_state failed: {e}")

    @filter.command("进店")
    async def cmd_enter(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = event.get_sender_name()

        msg = self.mgr.enter_store(gid, uid, name)
        await self._save_state()
        yield event.plain_result(msg)

    @filter.command("离店")
    async def cmd_leave(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = event.get_sender_name()

        msg = self.mgr.leave_store(gid, uid, name)
        await self._save_state()
        yield event.plain_result(msg)

    @filter.command("上机")
    async def cmd_on(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = event.get_sender_name()

        if not self.mgr.ensure_in_store(gid, uid):
            yield event.plain_result("您当前未在店内")
            return

        machine_id = parse_machine_arg(event.message_str, "上机")
        if machine_id is None:
            yield event.plain_result(f"用户{fmt_user(name, uid)}\n请输入机器标识：mai chu ong")
            return

        msg = self.mgr.on_machine(gid, uid, name, machine_id)
        await self._save_state()
        yield event.plain_result(msg)

    @filter.command("暂停")
    async def cmd_pause(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = event.get_sender_name()

        if not self.mgr.ensure_in_store(gid, uid):
            yield event.plain_result("您当前未在店内")
            return

        msg = self.mgr.pause(gid, uid, name)
        await self._save_state()
        yield event.plain_result(msg)

    @filter.command("计时")
    async def cmd_timing(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = event.get_sender_name()

        if not self.mgr.ensure_in_store(gid, uid):
            yield event.plain_result("您当前未在店内")
            return

        yield event.plain_result(self.mgr.timing(gid, uid, name))

    @filter.command("下机")
    async def cmd_off(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = event.get_sender_name()

        if not self.mgr.ensure_in_store(gid, uid):
            yield event.plain_result("您当前未在店内")
            return

        msg = self.mgr.off_machine(gid, uid, name)
        await self._save_state()
        yield event.plain_result(msg)

    @filter.command("窝几")
    async def cmd_wojis(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        # 修改点：/窝几 不需要进店也可查询
        yield event.plain_result(self.mgr.wojis(gid))
