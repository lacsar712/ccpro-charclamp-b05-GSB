from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, User, utcnow
from charclamp.domain.rules import (
    SHIFT_WINDOW_MESSAGE,
    RuleError,
    as_utc,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
    validate_shift_start,
)
from charclamp.infra.db import SessionLocal
from charclamp.infra.security import verify_password

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _set_flash(request: Request, message: str, category: str = "ok") -> None:
    data = dict(request.session or {})
    data["flash"] = message
    data["flash_cat"] = category
    request.set_session(data)


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    data = dict(request.session or {})
    message = data.pop("flash", None)
    category = data.pop("flash_cat", None)
    if message is not None or category is not None:
        request.set_session(data)
    return message, category


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_started_at(raw: str | None) -> datetime:
    raw = (raw or "").strip()
    if not raw:
        return utcnow()
    # datetime-local 提交的是无时区时间，按服务器 UTC 处理。
    return as_utc(datetime.fromisoformat(raw))


def _dt_local_input_value(dt: datetime) -> str:
    return as_utc(dt).strftime("%Y-%m-%dT%H:%M")


def _floor_minute(dt: datetime) -> datetime:
    return as_utc(dt).replace(second=0, microsecond=0)


def shift_input_bounds(
    previous: datetime | None,
    next_: datetime | None,
    now: datetime,
) -> tuple[str, str]:
    """
    抽屉 datetime-local 的 min/max（属性为闭区间，规则为严格不等）：
    下界 = 上一班整点分钟 + 1 分钟；上界 = 服务器时刻 +10 分钟，
    改写时还要让开下一班。仅前端即时约束，服务器端仍权威校验。
    """
    min_dt = _floor_minute(previous) + timedelta(minutes=1) if previous else None
    max_dt = _floor_minute(now + timedelta(minutes=10))
    if next_ is not None:
        nxt = as_utc(next_)
        next_cap = _floor_minute(nxt)
        if next_cap == nxt:  # 下一班恰落在整分钟，严格早于它需再让一分钟
            next_cap -= timedelta(minutes=1)
        max_dt = min(max_dt, next_cap)
    return (
        _dt_local_input_value(min_dt) if min_dt else "",
        _dt_local_input_value(max_dt),
    )


def latest_started(shifts: list[BurnShift]) -> datetime | None:
    if not shifts:
        return None
    return as_utc(max(shifts, key=lambda s: as_utc(s.started_at)).started_at)


async def _load_timeline_context(clamp_id: int | None = None) -> dict[str, Any]:
    async with SessionLocal() as db:
        clamps = list(
            (
                await db.execute(
                    select(Clamp)
                    .options(selectinload(Clamp.site), selectinload(Clamp.shifts))
                    .order_by(Clamp.code)
                )
            )
            .scalars()
            .all()
        )
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
    }


class AuthController(Controller):
    path = ""
    tags = ["auth"]

    @get("/login", media_type=MediaType.HTML)
    async def login_page(self, request: Request) -> Template:
        flash, flash_cat = _pop_flash(request)
        return Template(
            template_name="login.html",
            context={"flash": flash, "flash_cat": flash_cat},
        )

    @post("/login")
    async def login(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user or not verify_password(password, user.password_hash):
                request.set_session({"flash": "用户名或密码错误", "flash_cat": "error"})
                return Redirect("/login")
            request.set_session({"user_id": user.id})
        return Redirect("/")

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.clear_session()
        return Redirect("/login")


class TimelineController(Controller):
    path = ""
    tags = ["timeline"]

    @get("/", media_type=MediaType.HTML)
    async def timeline(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="timeline.html",
            context={
                **ctx,
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @get("/timeline/partial", media_type=MediaType.HTML)
    async def timeline_partial(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="partials/board.html",
            context={
                **ctx,
                "user": request.user,
            },
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        now = utcnow()
        async with SessionLocal() as db:
            clamps = list(
                (
                    await db.execute(
                        select(Clamp)
                        .options(selectinload(Clamp.shifts))
                        .order_by(Clamp.code)
                    )
                )
                .scalars()
                .all()
            )
            latest_by_clamp = {c.id: latest_started(c.shifts) for c in clamps}
        preselect = clamp_id
        min_dt = latest_by_clamp.get(preselect) if preselect is not None else None
        min_value, max_value = shift_input_bounds(min_dt, None, now)
        clamp_min_values = {
            c.id: shift_input_bounds(latest_by_clamp.get(c.id), None, now)[0]
            for c in clamps
        }
        return self._shift_drawer_template(
            preselect_clamp_id=preselect,
            clamps=clamps,
            started_bounds=(min_value, max_value),
            clamp_min_values=clamp_min_values,
            user=request.user,
        )

    @get("/drawer/shift-edit/{shift_id:int}", media_type=MediaType.HTML)
    async def drawer_shift_edit(self, request: Request, shift_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        now = utcnow()
        async with SessionLocal() as db:
            result = await db.execute(
                select(BurnShift)
                .where(BurnShift.id == shift_id)
                .options(selectinload(BurnShift.clamp).selectinload(Clamp.shifts))
            )
            shift = result.scalar_one_or_none()
            if not shift:
                return Redirect("/")
            current = as_utc(shift.started_at)
            other_starts = sorted(
                as_utc(s.started_at) for s in shift.clamp.shifts if s.id != shift.id
            )
            before = [t for t in other_starts if t < current]
            after = [t for t in other_starts if t > current]
            min_dt = before[-1] if before else None
            max_dt_for_next = after[0] if after else None
            min_value, max_value = shift_input_bounds(min_dt, max_dt_for_next, now)
            shift_started = current
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "mode": "edit",
                "shift": shift,
                "shift_id": shift.id,
                "clamp": shift.clamp,
                "preselect_clamp_id": shift.clamp_id,
                "started_at_value": _dt_local_input_value(shift_started),
                "peak_value": "" if shift.peak_temp_c is None else shift.peak_temp_c,
                "grade_value": shift.charcoal_grade,
                "notes_value": shift.notes,
                "min_started_at": min_value,
                "max_started_at": max_value,
                "shift_window_message": SHIFT_WINDOW_MESSAGE,
                "user": request.user,
            },
        )

    @staticmethod
    def _shift_drawer_template(
        *,
        clamps: list[Clamp],
        preselect_clamp_id: int | None,
        started_bounds: tuple[str, str],
        clamp_min_values: dict[int, str],
        user: User,
    ) -> Template:
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "mode": "new",
                "clamps": clamps,
                "preselect_clamp_id": preselect_clamp_id,
                "started_at_value": "",
                "peak_value": "",
                "grade_value": "B",
                "notes_value": "",
                "min_started_at": started_bounds[0],
                "max_started_at": started_bounds[1],
                "clamp_min_values": clamp_min_values,
                "shift_window_message": SHIFT_WINDOW_MESSAGE,
                "user": user,
            },
        )

    @get("/drawer/clamp/{clamp_id:int}", media_type=MediaType.HTML)
    async def drawer_clamp(self, request: Request, clamp_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts), selectinload(Clamp.site))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "user": request.user,
            },
        )


class ShiftController(Controller):
    path = "/shifts"
    tags = ["shifts"]

    @post("/new")
    async def create_shift(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        try:
            clamp_id = int(data["clamp_id"])
        except (KeyError, TypeError, ValueError):
            _set_flash(request, SHIFT_WINDOW_MESSAGE, "error")
            return Redirect("/")
        started_at = _parse_started_at(data.get("started_at"))
        peak = _parse_optional_float((data.get("peak_temp_c") or "").strip())
        grade = (data.get("charcoal_grade") or "B").strip() or "B"
        notes = (data.get("notes") or "").strip()
        async with SessionLocal() as db:
            try:
                # 先锁窑行：两人同时往同一窑插班时在此排队，
                # 后入者拿到的「上一班」一定包含先入者刚写的那笔。
                clamp = (
                    await db.execute(
                        select(Clamp).where(Clamp.id == clamp_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if clamp is None:
                    _set_flash(request, SHIFT_WINDOW_MESSAGE, "error")
                    return Redirect("/")
                prev = (
                    await db.execute(
                        select(BurnShift.started_at)
                        .where(BurnShift.clamp_id == clamp_id)
                        .order_by(BurnShift.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                validate_shift_start(started_at, prev)
                shift = BurnShift(
                    clamp_id=clamp_id,
                    started_at=started_at,
                    peak_temp_c=peak,
                    charcoal_grade=grade,
                    notes=notes,
                )
                db.add(shift)
                # 已码窑无班时写第一班，窑态随之改为焖烧中。
                if clamp.status == Clamp.STATUS_STACKED:
                    clamp.status = Clamp.STATUS_BURNING
                await db.flush()
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={clamp_id}")
            except IntegrityError:
                # 行锁放行后的最终兜底：同一夹缝时刻只许一笔入库。
                await db.rollback()
                _set_flash(request, SHIFT_WINDOW_MESSAGE, "error")
                return Redirect(f"/?clamp_id={clamp_id}")
        _set_flash(request, "焖烧班次已登记", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")

    @post("/{shift_id:int}/edit")
    async def edit_shift(
        self,
        request: Request,
        shift_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        started_at = _parse_started_at(data.get("started_at"))
        peak = _parse_optional_float((data.get("peak_temp_c") or "").strip())
        grade = (data.get("charcoal_grade") or "B").strip() or "B"
        notes = (data.get("notes") or "").strip()
        clamp_id: int | None = None
        async with SessionLocal() as db:
            try:
                shift = (
                    await db.execute(
                        select(BurnShift)
                        .where(BurnShift.id == shift_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if shift is None:
                    _set_flash(request, SHIFT_WINDOW_MESSAGE, "error")
                    return Redirect("/")
                clamp_id = shift.clamp_id
                # 改写同样锁窑行；窗口为「本班的上一班」与「本班的下一班」之间。
                await db.execute(
                    select(Clamp).where(Clamp.id == clamp_id).with_for_update()
                )
                prev = (
                    await db.execute(
                        select(BurnShift.started_at)
                        .where(
                            BurnShift.clamp_id == clamp_id,
                            BurnShift.id != shift_id,
                            BurnShift.started_at < shift.started_at,
                        )
                        .order_by(BurnShift.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                next_start = (
                    await db.execute(
                        select(BurnShift.started_at)
                        .where(
                            BurnShift.clamp_id == clamp_id,
                            BurnShift.id != shift_id,
                            BurnShift.started_at > shift.started_at,
                        )
                        .order_by(BurnShift.started_at.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                validate_shift_start(started_at, prev, next_started_at=next_start)
                shift.started_at = started_at
                shift.peak_temp_c = peak
                shift.charcoal_grade = grade
                shift.notes = notes
                await db.flush()
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={clamp_id}")
            except IntegrityError:
                await db.rollback()
                _set_flash(request, SHIFT_WINDOW_MESSAGE, "error")
                return Redirect(f"/?clamp_id={clamp_id}")
        _set_flash(request, "焖烧班次已改写", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")


class ClampController(Controller):
    path = "/clamps"
    tags = ["clamps"]

    @post("/{clamp_id:int}/status")
    async def set_status(
        self,
        request: Request,
        clamp_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        new_status = (data.get("status") or "").strip()
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")
