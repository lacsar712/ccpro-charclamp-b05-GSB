"""炭窑焖烧志业务规则。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from charclamp.domain.models import BurnShift, Clamp
from charclamp.domain.models import utcnow

MIN_PEAK_TEMP_FOR_DRAWN = 400.0

# 班次开始时刻允许超前服务器时刻的上限。
SHIFT_START_FUTURE_TOLERANCE = timedelta(minutes=10)

# 新建与改写共用的唯一一句中文：抽屉提示、前端即时校验、保存拦截都引用它。
SHIFT_WINDOW_MESSAGE = (
    "班次开始时刻不得晚于当前时刻 10 分钟以上，也不得早于该窑上一班开始。"
)


class RuleError(ValueError):
    """业务规则校验失败。"""


def as_utc(dt: datetime) -> datetime:
    """把无时区的时间戳按 UTC 处理，再统一成 UTC 便于比较。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


def validate_shift_start(
    started_at: datetime,
    previous_started_at: datetime | None,
    now: datetime | None = None,
    next_started_at: datetime | None = None,
) -> None:
    """
    班次开始时刻窗口（新建与改写一致）：
    1. 不得晚于服务器时刻 10 分钟以上；
    2. 该窑已有班次时，必须严格晚于上一班的开始时刻；
    3. 改写时还必须严格早于下一班（否则下一班反而早于它的上一班，全局序被破坏）。
    第一班无「上一班」可比对，只受第 1 条约束。
    """
    now = as_utc(now or utcnow())
    started_at = as_utc(started_at)
    if started_at - now > SHIFT_START_FUTURE_TOLERANCE:
        raise RuleError(SHIFT_WINDOW_MESSAGE)
    if previous_started_at is not None and started_at <= as_utc(previous_started_at):
        raise RuleError(SHIFT_WINDOW_MESSAGE)
    if next_started_at is not None and started_at >= as_utc(next_started_at):
        raise RuleError(SHIFT_WINDOW_MESSAGE)


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
