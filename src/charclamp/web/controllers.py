from __future__ import annotations

import json
from typing import Any

from markupsafe import Markup
from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, User
from charclamp.domain.rules import (
    MSG_DUP_START,
    MSG_NOT_AFTER_PREV,
    MSG_START_TOO_LATE,
    RuleError,
    assert_can_set_clamp_status,
    assert_shift_window,
    can_mark_clamp_drawn,
    latest_shift_for_clamp,
    parse_started_at,
    predecessor_shift,
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


def _parse_peak(raw: str | None) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    return float(text)


def _datetime_local_value(dt: Any) -> str:
    """datetime-local 输入框所需的 ``YYYY-MM-DDTHH:MM``（按存储的 UTC）。"""
    return dt.strftime("%Y-%m-%dT%H:%M")


async def _latest_start_map(db: Any) -> dict[int, Any]:
    clamps = list(
        (
            await db.execute(
                select(Clamp).options(selectinload(Clamp.shifts)).order_by(Clamp.code)
            )
        )
        .scalars()
        .all()
    )
    latest_map: dict[int, Any] = {}
    for clamp in clamps:
        latest = latest_shift_for_clamp(clamp)
        if latest is not None:
            latest_map[clamp.id] = latest.started_at
    return clamps, latest_map


async def _locked_clamp(db: Any, clamp_id: int) -> Clamp | None:
    """行级锁定该窑，串行化同一窑的并发插班，并一并取其全部班次。"""
    return (
        await db.execute(
            select(Clamp)
            .where(Clamp.id == clamp_id)
            .with_for_update()
            .options(selectinload(Clamp.shifts))
        )
    ).scalar_one_or_none()


def _shift_form_js(latest_map: dict[int, Any], clamp_id: int | None) -> dict[str, str]:
    """抽屉内前端校验所需的 JS 字面量（中文文案与服务端逐字一致）。"""
    latest_json = {str(cid): ts.isoformat() for cid, ts in latest_map.items()}
    return {
        "latest_json": Markup(json.dumps(latest_json, ensure_ascii=False)),
        "clamp_id_json": Markup(json.dumps(clamp_id)),
        "msg_late_json": Markup(json.dumps(MSG_START_TOO_LATE, ensure_ascii=False)),
        "msg_prev_json": Markup(json.dumps(MSG_NOT_AFTER_PREV, ensure_ascii=False)),
    }


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
        async with SessionLocal() as db:
            clamps, latest_map = await _latest_start_map(db)
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "latest_map": latest_map,
                "shift": None,
                "user": request.user,
                "msg_start_too_late": MSG_START_TOO_LATE,
                "msg_not_after_prev": MSG_NOT_AFTER_PREV,
                **_shift_form_js(latest_map, clamp_id),
            },
        )

    @get("/drawer/shift/{shift_id:int}", media_type=MediaType.HTML)
    async def drawer_shift_edit(self, request: Request, shift_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db:
            shift = (
                await db.execute(
                    select(BurnShift)
                    .where(BurnShift.id == shift_id)
                    .options(selectinload(BurnShift.clamp).selectinload(Clamp.shifts))
                )
            ).scalar_one_or_none()
            if shift is None:
                return Redirect("/")
            clamps, _ = await _latest_start_map(db)
            # 改写时上一班指按时间紧邻本班之前的那一班；本班若已是最早一班则无下界。
            predecessor = predecessor_shift(shift.clamp.shifts, shift.id)
            latest_map = (
                {shift.clamp_id: predecessor.started_at} if predecessor else {}
            )
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": shift.clamp_id,
                "latest_map": latest_map,
                "shift": shift,
                "started_value": _datetime_local_value(shift.started_at),
                "user": request.user,
                "msg_start_too_late": MSG_START_TOO_LATE,
                "msg_not_after_prev": MSG_NOT_AFTER_PREV,
                **_shift_form_js(latest_map, shift.clamp_id),
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


def _read_shift_form(data: dict[str, Any]) -> tuple[Any, float | None, str, str]:
    started_at = parse_started_at(data.get("started_at"))  # 可能抛 RuleError
    peak = _parse_peak(data.get("peak_temp_c"))
    grade = (data.get("charcoal_grade") or "B").strip()
    notes = (data.get("notes") or "").strip()
    return started_at, peak, grade, notes


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
            _set_flash(request, "请先选择炭窑", "error")
            return Redirect("/")
        try:
            started_at, peak, grade, notes = _read_shift_form(data)
        except RuleError as exc:
            _set_flash(request, str(exc), "error")
            return Redirect(f"/?clamp_id={clamp_id}")
        except ValueError:
            _set_flash(request, "峰值温度需为数字", "error")
            return Redirect(f"/?clamp_id={clamp_id}")

        async with SessionLocal() as db:
            try:
                clamp = await _locked_clamp(db, clamp_id)
                if clamp is None:
                    await db.rollback()
                    return Redirect("/")
                latest = latest_shift_for_clamp(clamp)
                # 锁内复核时间窗：已码窑无班时可写第一班，其后必须严格晚于上一班。
                assert_shift_window(
                    started_at, latest.started_at if latest else None
                )
                db.add(
                    BurnShift(
                        clamp_id=clamp_id,
                        started_at=started_at,
                        peak_temp_c=peak,
                        charcoal_grade=grade,
                        notes=notes,
                    )
                )
                # 已码窑写第一班即转入焖烧中。
                if clamp.status == Clamp.STATUS_STACKED:
                    clamp.status = Clamp.STATUS_BURNING
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={clamp_id}")
            except IntegrityError:
                # 两人几乎同时往同一夹缝时刻插班：唯一约束只放一笔，另一笔中文挡下。
                await db.rollback()
                _set_flash(request, MSG_DUP_START, "error")
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
        try:
            started_at, peak, grade, notes = _read_shift_form(data)
        except RuleError as exc:
            _set_flash(request, str(exc), "error")
            return Redirect("/")
        except ValueError:
            _set_flash(request, "峰值温度需为数字", "error")
            return Redirect("/")

        async with SessionLocal() as db:
            try:
                # 与新建同一把窑行锁：改写与插班在同一窑上串行。
                anchor = (
                    await db.execute(
                        select(BurnShift.clamp_id).where(BurnShift.id == shift_id)
                    )
                ).scalar_one_or_none()
                if anchor is None:
                    await db.rollback()
                    return Redirect("/")
                clamp = await _locked_clamp(db, anchor)
                target = next((s for s in clamp.shifts if s.id == shift_id), None)
                if target is None:
                    await db.rollback()
                    return Redirect("/")
                predecessor = predecessor_shift(clamp.shifts, shift_id)
                assert_shift_window(
                    started_at, predecessor.started_at if predecessor else None
                )
                target.started_at = started_at
                target.peak_temp_c = peak
                target.charcoal_grade = grade
                target.notes = notes
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={anchor}")
            except IntegrityError:
                await db.rollback()
                _set_flash(request, MSG_DUP_START, "error")
                return Redirect(f"/?clamp_id={anchor}")
        _set_flash(request, "焖烧班次已改写", "ok")
        return Redirect(f"/?clamp_id={anchor}")


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
