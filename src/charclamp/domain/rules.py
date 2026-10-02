"""炭窑焖烧志业务规则。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from charclamp.domain.models import BurnShift, Clamp, utcnow

MIN_PEAK_TEMP_FOR_DRAWN = 400.0

# 班次开始时刻允许领先服务器时刻的上限。
MAX_START_AHEAD = timedelta(minutes=10)

# —— 班次开始时刻时间窗：新建与改写（抽屉提示语与保存拦截）共用同一句中文 ——
MSG_START_TOO_LATE = "班次开始时刻不得晚于服务器当前时刻 10 分钟以上"
MSG_NOT_AFTER_PREV = "班次开始时刻必须严格晚于该窑上一班的开始时刻"
MSG_DUP_START = "该窑这一开始时刻的班次已登记，请勿重复写入同一夹缝时刻"
MSG_INVALID_TIME = "开始时间格式无效，请重新选择"


class RuleError(ValueError):
    """业务规则校验失败。"""


def parse_started_at(raw: str | None) -> datetime:
    """解析表单提交的开始时刻；浏览器 datetime-local 为朴素时间，按 UTC 处理。"""
    text = (raw or "").strip()
    if not text:
        return utcnow()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuleError(MSG_INVALID_TIME) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_shift_window(
    started_at: datetime,
    latest_started_at: datetime | None,
    now: datetime | None = None,
) -> str | None:
    """
    班次开始时刻时间窗校验，返回中文错误信息；通过返回 None。

    规则：
    1. 开始时刻不得晚于服务器当前时刻 10 分钟以上；
    2. 该窑已有班次时，开始时刻必须严格晚于最近一班。
       （已码窑尚无班次时可写第一班，不设下界。）
    """
    now = now or utcnow()
    if started_at > now + MAX_START_AHEAD:
        return MSG_START_TOO_LATE
    if latest_started_at is not None and started_at <= latest_started_at:
        return MSG_NOT_AFTER_PREV
    return None


def assert_shift_window(
    started_at: datetime,
    latest_started_at: datetime | None,
    now: datetime | None = None,
) -> None:
    message = validate_shift_window(started_at, latest_started_at, now)
    if message:
        raise RuleError(message)


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


def ordered_shifts(shifts: list[BurnShift]) -> list[BurnShift]:
    return sorted(shifts, key=lambda s: s.started_at)


def predecessor_shift(shifts: list[BurnShift], shift_id: int) -> BurnShift | None:
    """
    改写时的「上一班」：按开始时刻排序后，紧邻被改班次之前的那一班。
    被改班次本身已是最早一班时返回 None（下方再无下界）。
    """
    ordered = ordered_shifts(shifts)
    index = next((i for i, shift in enumerate(ordered) if shift.id == shift_id), None)
    if index is None or index == 0:
        return None
    return ordered[index - 1]


def can_mark_clamp_drawn(clamp: Clamp) -> tuple[bool, str]:
    """
    炭窑转为「已出炭」(drawn) 的前提：
    最近一条焖烧班次的峰值温度已记录，且 >= 400℃。
    """
    latest = latest_shift_for_clamp(clamp)
    if latest is None:
        return False, "该窑尚无焖烧班次，不能标记为已出炭"
    if latest.peak_temp_c is None:
        return False, "最近班次尚未记录峰值温度，不能标记为已出炭"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近班次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出炭",
        )
    return True, ""


def assert_can_set_clamp_status(clamp: Clamp, new_status: str) -> None:
    allowed = {Clamp.STATUS_STACKED, Clamp.STATUS_BURNING, Clamp.STATUS_DRAWN}
    if new_status not in allowed:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Clamp.STATUS_DRAWN:
        ok, msg = can_mark_clamp_drawn(clamp)
        if not ok:
            raise RuleError(msg)
