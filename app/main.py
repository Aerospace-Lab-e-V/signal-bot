import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    DEVELOPMENT_USER,
    OAuthError,
    auth_redirect,
    build_oauth,
    is_authorized,
    oidc_debug_claims,
    require_user,
    user_display_name,
)
from app.config import settings
from app.database import get_db, init_db
from app.models import LinkedGroup, RetryEntry, Schedule, SendAttempt
from app.scheduler import create_scheduler, get_timezone, sync_schedule_jobs
from signal_api import SignalAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Signal Scheduler")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")
oauth = build_oauth(settings)


def create_signal_api() -> SignalAPI:
    return SignalAPI(
        sender_number=settings.signal_sender_number,
        signal_cli_path=settings.signal_cli_path,
        signal_cli_data_dir=settings.signal_cli_data_dir,
        command_timeout_seconds=settings.signal_cli_timeout_seconds,
        receive_timeout_seconds=settings.signal_receive_timeout_seconds,
    )


def format_datetime(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return value.strftime("%Y-%m-%d %H:%M")


templates.env.filters["datetime"] = format_datetime


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return auth_redirect(request)

    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "title": f"Error {exc.status_code}",
            "message": exc.detail,
        },
        status_code=exc.status_code,
    )


@app.on_event("startup")
def startup():
    init_db()
    scheduler = create_scheduler()
    scheduler.start()
    sync_schedule_jobs(scheduler)
    app.state.scheduler = scheduler
    logger.info("Signal Scheduler web app started")


@app.on_event("shutdown")
def shutdown():
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler:
        scheduler.shutdown(wait=False)


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def template(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
    user = request.session.get("user")
    if not user and settings.auth_bypass_for_development:
        user = DEVELOPMENT_USER

    base = {
        "request": request,
        "user": user,
        "user_display_name": user_display_name(user) if user else "",
        "settings": settings,
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def callback_url(request: Request) -> str:
    if settings.app_base_url:
        return f"{settings.app_base_url.rstrip('/')}/auth/callback"
    return str(request.url_for("auth_callback"))


def normalize_group(group: Any) -> dict[str, str] | None:
    if isinstance(group, str):
        return {"signal_group_id": group, "name": group}

    if not isinstance(group, dict):
        return None

    signal_group_id = (
        group.get("id")
        or group.get("groupId")
        or group.get("group_id")
        or group.get("internal_id")
        or group.get("group")
    )
    if not signal_group_id:
        return None

    name = group.get("name") or group.get("title") or group.get("description") or signal_group_id
    return {"signal_group_id": signal_group_id, "name": name}


def form_value(form: dict[str, Any], key: str, default: str = "") -> str:
    value = form.get(key, default)
    if value is None:
        return ""
    return str(value)


def valid_time(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    return True


def parse_schedule_form(form: dict[str, Any], db: Session) -> tuple[dict[str, Any], list[str]]:
    errors = []
    try:
        group_id = int(form_value(form, "group_id", "0") or "0")
    except ValueError:
        group_id = 0

    values = {
        "group_id": group_id,
        "message": form_value(form, "message").strip(),
        "schedule_type": form_value(form, "schedule_type", "weekly"),
        "run_at_date": form_value(form, "run_at_date"),
        "run_at_time": form_value(form, "run_at_time"),
        "day_of_week": form_value(form, "day_of_week", "0"),
        "day_of_month": form_value(form, "day_of_month", "1"),
        "time_of_day": form_value(form, "time_of_day"),
        "timezone": form_value(form, "timezone", settings.app_timezone),
        "enabled": form.get("enabled") in ("on", "true", "1", True),
    }

    group = db.get(LinkedGroup, values["group_id"])
    if not group:
        errors.append("Choose a linked group.")

    if not values["message"]:
        errors.append("Message text is required.")

    try:
        tz = ZoneInfo(values["timezone"])
    except ZoneInfoNotFoundError:
        errors.append("Timezone is invalid.")
        tz = get_timezone(settings.app_timezone)

    schedule_type = values["schedule_type"]
    if schedule_type not in {"one_off", "weekly", "monthly"}:
        errors.append("Schedule type is invalid.")

    values["run_at"] = None
    values["day_of_week_int"] = None
    values["day_of_month_int"] = None

    if schedule_type == "one_off":
        try:
            local_run_at = datetime.fromisoformat(f"{values['run_at_date']}T{values['run_at_time']}")
            values["run_at"] = local_run_at.replace(tzinfo=tz)
        except ValueError:
            errors.append("One-off schedules need a valid date and time.")
    elif schedule_type == "weekly":
        try:
            day_of_week = int(values["day_of_week"])
            if day_of_week < 0 or day_of_week > 6:
                raise ValueError
            values["day_of_week_int"] = day_of_week
        except ValueError:
            errors.append("Weekly schedules need a weekday.")

        if not values["time_of_day"] or not valid_time(values["time_of_day"]):
            errors.append("Weekly schedules need a valid time.")
    elif schedule_type == "monthly":
        try:
            day_of_month = int(values["day_of_month"])
            if day_of_month < 1 or day_of_month > 31:
                raise ValueError
            values["day_of_month_int"] = day_of_month
        except ValueError:
            errors.append("Monthly schedules need a day from 1 to 31.")

        if not values["time_of_day"] or not valid_time(values["time_of_day"]):
            errors.append("Monthly schedules need a valid time.")

    return values, errors


def apply_schedule_values(schedule: Schedule, values: dict[str, Any]):
    schedule.group_id = values["group_id"]
    schedule.message = values["message"]
    schedule.schedule_type = values["schedule_type"]
    schedule.run_at = values["run_at"]
    schedule.day_of_week = values["day_of_week_int"]
    schedule.day_of_month = values["day_of_month_int"]
    schedule.time_of_day = values["time_of_day"] if values["schedule_type"] != "one_off" else None
    schedule.timezone = values["timezone"]
    schedule.enabled = values["enabled"]


def schedule_form_context(db: Session, form: dict[str, Any] | None = None, schedule: Schedule | None = None):
    groups = db.execute(select(LinkedGroup).order_by(LinkedGroup.name)).scalars().all()
    form = form or {}
    if schedule and not form:
        run_at_date = schedule.run_at.strftime("%Y-%m-%d") if schedule.run_at else ""
        run_at_time = schedule.run_at.strftime("%H:%M") if schedule.run_at else ""
        form = {
            "group_id": schedule.group_id,
            "message": schedule.message,
            "schedule_type": schedule.schedule_type,
            "run_at_date": run_at_date,
            "run_at_time": run_at_time,
            "day_of_week": schedule.day_of_week if schedule.day_of_week is not None else 0,
            "day_of_month": schedule.day_of_month if schedule.day_of_month is not None else 1,
            "time_of_day": schedule.time_of_day or "",
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
        }

    return {
        "groups": groups,
        "form": form,
        "weekdays": [
            (0, "Monday"),
            (1, "Tuesday"),
            (2, "Wednesday"),
            (3, "Thursday"),
            (4, "Friday"),
            (5, "Saturday"),
            (6, "Sunday"),
        ],
    }


@app.get("/auth/login")
async def login(request: Request):
    if settings.auth_bypass_for_development:
        return redirect("/")

    if not settings.oidc_enabled:
        raise HTTPException(status_code=503, detail="OIDC is not configured.")
    return await oauth.keycloak.authorize_redirect(request, callback_url(request))


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.keycloak.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.keycloak.parse_id_token(request, token)

    user = dict(userinfo)
    if settings.oidc_debug_claims:
        logger.info("OIDC login claims: %s", oidc_debug_claims(user, settings))

    if not is_authorized(user, settings):
        logger.warning("Rejected OIDC login: %s", oidc_debug_claims(user, settings))
        raise HTTPException(status_code=403, detail="Your account is not allowed to use this app.")

    request.session["user"] = user
    next_url = request.session.pop("next_url", "/")
    return redirect(next_url)


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return redirect(settings.logout_redirect_url or "/")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    group_count = db.scalar(select(func.count(LinkedGroup.id)))
    schedule_count = db.scalar(select(func.count(Schedule.id)))
    active_schedule_count = db.scalar(select(func.count(Schedule.id)).where(Schedule.enabled.is_(True)))
    retry_count = db.scalar(select(func.count(RetryEntry.id)).where(RetryEntry.status == "pending"))
    recent_attempts = db.execute(
        select(SendAttempt)
        .options(joinedload(SendAttempt.group), joinedload(SendAttempt.schedule))
        .order_by(SendAttempt.attempted_at.desc())
        .limit(8)
    ).scalars().all()
    return template(
        request,
        "dashboard.html",
        {
            "group_count": group_count,
            "schedule_count": schedule_count,
            "active_schedule_count": active_schedule_count,
            "retry_count": retry_count,
            "recent_attempts": recent_attempts,
        },
    )


@app.get("/groups", response_class=HTMLResponse)
def groups(request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    linked_groups = db.execute(
        select(LinkedGroup)
        .options(selectinload(LinkedGroup.schedules))
        .order_by(LinkedGroup.name)
    ).scalars().all()
    return template(request, "groups.html", {"linked_groups": linked_groups})


@app.get("/groups/import", response_class=HTMLResponse)
def import_groups(request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    signal = create_signal_api()
    result = signal.list_groups()
    linked_ids = {
        row[0]
        for row in db.execute(select(LinkedGroup.signal_group_id)).all()
    }
    available_groups = []
    error = None

    if result.ok:
        for group in result.data:
            normalized = normalize_group(group)
            if normalized:
                normalized["linked"] = normalized["signal_group_id"] in linked_ids
                available_groups.append(normalized)
    else:
        error = result.error or "Could not load groups from Signal."

    return template(
        request,
        "groups_import.html",
        {"available_groups": available_groups, "error": error},
        status_code=502 if error else 200,
    )


@app.post("/groups/import")
def save_imported_group(
    signal_group_id: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    group = db.execute(
        select(LinkedGroup).where(LinkedGroup.signal_group_id == signal_group_id)
    ).scalar_one_or_none()
    if group:
        group.name = name.strip() or signal_group_id
    else:
        db.add(LinkedGroup(signal_group_id=signal_group_id, name=name.strip() or signal_group_id))
    db.commit()
    return redirect("/groups")


@app.post("/groups/{group_id}/delete")
def delete_group(group_id: int, request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    group = db.get(LinkedGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found.")

    schedule_ids = [
        row[0]
        for row in db.execute(select(Schedule.id).where(Schedule.group_id == group.id)).all()
    ]
    if schedule_ids:
        db.execute(
            update(SendAttempt)
            .where(SendAttempt.schedule_id.in_(schedule_ids))
            .values(schedule_id=None)
        )
        db.execute(
            update(RetryEntry)
            .where(RetryEntry.schedule_id.in_(schedule_ids))
            .values(schedule_id=None)
        )
        for schedule in db.execute(select(Schedule).where(Schedule.id.in_(schedule_ids))).scalars():
            db.delete(schedule)

    db.execute(update(SendAttempt).where(SendAttempt.group_id == group.id).values(group_id=None))
    db.execute(update(RetryEntry).where(RetryEntry.group_id == group.id).values(group_id=None))
    db.delete(group)
    db.commit()
    sync_schedule_jobs(request.app.state.scheduler)
    return redirect("/groups")


@app.get("/schedules", response_class=HTMLResponse)
def schedules(request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    rows = db.execute(
        select(Schedule)
        .options(joinedload(Schedule.group))
        .order_by(Schedule.enabled.desc(), Schedule.id.desc())
    ).scalars().all()
    return template(request, "schedules.html", {"schedules": rows})


@app.get("/schedules/new", response_class=HTMLResponse)
def new_schedule(request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    context = schedule_form_context(
        db,
        form={
            "schedule_type": "weekly",
            "day_of_week": 3,
            "day_of_month": 1,
            "time_of_day": "15:00",
            "timezone": settings.app_timezone,
            "enabled": True,
        },
    )
    context.update({"title": "New Schedule", "action": "/schedules", "errors": []})
    return template(request, "schedule_form.html", context)


@app.post("/schedules")
def create_schedule(
    request: Request,
    group_id: str = Form(""),
    message: str = Form(""),
    schedule_type: str = Form("weekly"),
    run_at_date: str = Form(""),
    run_at_time: str = Form(""),
    day_of_week: str = Form("0"),
    day_of_month: str = Form("1"),
    time_of_day: str = Form(""),
    timezone: str = Form(settings.app_timezone),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    form = locals()
    values, errors = parse_schedule_form(form, db)
    if errors:
        context = schedule_form_context(db, form=form)
        context.update({"title": "New Schedule", "action": "/schedules", "errors": errors})
        return template(request, "schedule_form.html", context, status_code=400)

    schedule = Schedule(timezone=settings.app_timezone, message="", schedule_type="weekly", group_id=values["group_id"])
    apply_schedule_values(schedule, values)
    db.add(schedule)
    db.commit()
    sync_schedule_jobs(request.app.state.scheduler)
    return redirect("/schedules")


@app.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
def edit_schedule(schedule_id: int, request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    schedule = db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    context = schedule_form_context(db, schedule=schedule)
    context.update({"title": "Edit Schedule", "action": f"/schedules/{schedule.id}", "errors": []})
    return template(request, "schedule_form.html", context)


@app.post("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    request: Request,
    group_id: str = Form(""),
    message: str = Form(""),
    schedule_type: str = Form("weekly"),
    run_at_date: str = Form(""),
    run_at_time: str = Form(""),
    day_of_week: str = Form("0"),
    day_of_month: str = Form("1"),
    time_of_day: str = Form(""),
    timezone: str = Form(settings.app_timezone),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    schedule = db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")

    form = locals()
    values, errors = parse_schedule_form(form, db)
    if errors:
        context = schedule_form_context(db, form=form, schedule=schedule)
        context.update({"title": "Edit Schedule", "action": f"/schedules/{schedule.id}", "errors": errors})
        return template(request, "schedule_form.html", context, status_code=400)

    apply_schedule_values(schedule, values)
    db.commit()
    sync_schedule_jobs(request.app.state.scheduler)
    return redirect("/schedules")


@app.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int, request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    schedule = db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    schedule.enabled = not schedule.enabled
    db.commit()
    sync_schedule_jobs(request.app.state.scheduler)
    return redirect("/schedules")


@app.post("/schedules/{schedule_id}/delete")
def delete_schedule(schedule_id: int, request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    schedule = db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    db.delete(schedule)
    db.commit()
    sync_schedule_jobs(request.app.state.scheduler)
    return redirect("/schedules")


@app.get("/attempts", response_class=HTMLResponse)
def attempts(request: Request, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    rows = db.execute(
        select(SendAttempt)
        .options(joinedload(SendAttempt.group), joinedload(SendAttempt.schedule))
        .order_by(SendAttempt.attempted_at.desc())
        .limit(100)
    ).scalars().all()
    retries = db.execute(
        select(RetryEntry)
        .options(joinedload(RetryEntry.group), joinedload(RetryEntry.schedule))
        .order_by(RetryEntry.first_failed_at.desc())
        .limit(50)
    ).scalars().all()
    return template(request, "attempts.html", {"attempts": rows, "retries": retries})
