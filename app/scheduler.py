import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database import SessionLocal
from app.incoming import handle_signal_updates
from app.models import LinkedGroup, RetryEntry, Schedule, SendAttempt, utc_now
from signal_api import SignalAPI, SignalAPIResult

logger = logging.getLogger(__name__)


def create_signal_api() -> SignalAPI:
    return SignalAPI(
        sender_number=settings.signal_sender_number,
        signal_cli_path=settings.signal_cli_path,
        signal_cli_data_dir=settings.signal_cli_data_dir,
        command_timeout_seconds=settings.signal_cli_timeout_seconds,
        receive_timeout_seconds=settings.signal_receive_timeout_seconds,
    )


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.app_timezone)


def parse_time_of_day(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_trigger(schedule: Schedule):
    tz = get_timezone(schedule.timezone)

    if schedule.schedule_type == "one_off":
        if not schedule.run_at:
            raise ValueError("one-off schedule requires run_at")
        run_at = schedule.run_at
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=tz)
        return DateTrigger(run_date=run_at)

    if not schedule.time_of_day:
        raise ValueError("recurring schedule requires time_of_day")

    hour, minute = parse_time_of_day(schedule.time_of_day)
    if schedule.schedule_type == "weekly":
        if schedule.day_of_week is None:
            raise ValueError("weekly schedule requires day_of_week")
        return CronTrigger(
            day_of_week=str(schedule.day_of_week),
            hour=hour,
            minute=minute,
            timezone=tz,
        )

    if schedule.schedule_type == "monthly":
        if schedule.day_of_month is None:
            raise ValueError("monthly schedule requires day_of_month")
        return CronTrigger(
            day=str(schedule.day_of_month),
            hour=hour,
            minute=minute,
            timezone=tz,
        )

    raise ValueError(f"Unsupported schedule type: {schedule.schedule_type}")


def schedule_job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


def record_attempt(
    db,
    *,
    schedule_id: int | None,
    group_id: int | None,
    message: str,
    result: SignalAPIResult,
) -> SendAttempt:
    attempt = SendAttempt(
        schedule_id=schedule_id,
        group_id=group_id,
        message=message,
        status="success" if result.ok else "failed",
        error_message=result.error,
        response_status_code=result.status_code,
        attempted_at=utc_now(),
    )
    db.add(attempt)
    return attempt


def enqueue_retry(db, *, schedule: Schedule | None, group: LinkedGroup, message: str):
    now = utc_now()
    retry = RetryEntry(
        schedule_id=schedule.id if schedule else None,
        group_id=group.id,
        message=message,
        first_failed_at=now,
        last_attempt_at=now,
        attempts=1,
        expires_at=now + timedelta(hours=settings.retry_window_hours),
    )
    db.add(retry)
    logger.warning("Queued failed message for retry: group_id=%s schedule_id=%s", group.id, retry.schedule_id)


def execute_schedule(schedule_id: int):
    db = SessionLocal()
    try:
        schedule = db.execute(
            select(Schedule)
            .options(joinedload(Schedule.group))
            .where(Schedule.id == schedule_id)
        ).scalar_one_or_none()

        if not schedule or not schedule.enabled:
            return

        signal = create_signal_api()
        result = signal.send_message([schedule.group.signal_group_id], schedule.message)
        record_attempt(
            db,
            schedule_id=schedule.id,
            group_id=schedule.group.id,
            message=schedule.message,
            result=result,
        )
        schedule.last_run_at = utc_now()

        if result.ok and schedule.schedule_type == "one_off":
            schedule.enabled = False
        elif not result.ok:
            enqueue_retry(db, schedule=schedule, group=schedule.group, message=schedule.message)

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed while executing schedule %s", schedule_id)
    finally:
        db.close()


def process_retry_queue():
    db = SessionLocal()
    try:
        now = utc_now()
        retries = db.execute(
            select(RetryEntry)
            .options(joinedload(RetryEntry.group))
            .where(RetryEntry.status == "pending")
            .order_by(RetryEntry.first_failed_at)
        ).scalars().all()

        if not retries:
            return

        signal = create_signal_api()
        for retry in retries:
            if as_aware_utc(retry.expires_at) < now:
                retry.status = "expired"
                logger.error("Retry entry %s expired", retry.id)
                continue

            if not retry.group:
                retry.status = "failed"
                retry.last_attempt_at = now
                retry.attempts += 1
                continue

            result = signal.send_message([retry.group.signal_group_id], retry.message)
            record_attempt(
                db,
                schedule_id=retry.schedule_id,
                group_id=retry.group_id,
                message=retry.message,
                result=result,
            )
            retry.last_attempt_at = now
            retry.attempts += 1

            if result.ok:
                retry.status = "sent"

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed while processing retry queue")
    finally:
        db.close()


def process_signal_updates():
    signal = create_signal_api()
    result = signal.receive_updates()
    if not result.ok:
        logger.warning("Failed while receiving Signal updates: %s", result.error)
        return

    handled = handle_signal_updates(result.data)
    if handled:
        logger.info("Processed %d received Signal message(s)", handled)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=get_timezone(settings.app_timezone))
    scheduler.add_job(
        process_retry_queue,
        "interval",
        seconds=settings.retry_interval_seconds,
        id="retry-queue",
        replace_existing=True,
    )
    if settings.signal_receive_interval_seconds > 0:
        scheduler.add_job(
            process_signal_updates,
            "interval",
            seconds=settings.signal_receive_interval_seconds,
            id="signal-receive",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    return scheduler


def sync_schedule_jobs(scheduler: BackgroundScheduler):
    for job in scheduler.get_jobs():
        if job.id.startswith("schedule-"):
            scheduler.remove_job(job.id)

    db = SessionLocal()
    try:
        schedules = db.execute(
            select(Schedule).where(Schedule.enabled.is_(True)).order_by(Schedule.id)
        ).scalars().all()

        now = datetime.now(timezone.utc)
        for schedule in schedules:
            if schedule.schedule_type == "one_off" and schedule.run_at:
                run_at = schedule.run_at
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=get_timezone(schedule.timezone))
                if as_aware_utc(run_at) <= now:
                    continue

            try:
                scheduler.add_job(
                    execute_schedule,
                    trigger=build_trigger(schedule),
                    args=[schedule.id],
                    id=schedule_job_id(schedule.id),
                    replace_existing=True,
                    misfire_grace_time=300,
                )
            except Exception:
                logger.exception("Could not schedule job %s", schedule.id)
    finally:
        db.close()
