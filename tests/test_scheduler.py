from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import LinkedGroup, RetryEntry, Schedule, SendAttempt
from app.scheduler import build_trigger, create_scheduler, execute_schedule, process_retry_queue, process_signal_updates
from signal_api import SignalAPIResult


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def test_build_one_off_trigger():
    schedule = Schedule(
        group_id=1,
        message="Hello",
        schedule_type="one_off",
        run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        timezone="Europe/Berlin",
        enabled=True,
    )

    assert isinstance(build_trigger(schedule), DateTrigger)


def test_build_weekly_and_monthly_triggers():
    weekly = Schedule(
        group_id=1,
        message="Hello",
        schedule_type="weekly",
        day_of_week=3,
        time_of_day="15:00",
        timezone="Europe/Berlin",
        enabled=True,
    )
    monthly = Schedule(
        group_id=1,
        message="Hello",
        schedule_type="monthly",
        day_of_month=12,
        time_of_day="09:30",
        timezone="Europe/Berlin",
        enabled=True,
    )

    assert isinstance(build_trigger(weekly), CronTrigger)
    assert isinstance(build_trigger(monthly), CronTrigger)


def test_execute_schedule_records_success_and_disables_one_off(monkeypatch, tmp_path):
    TestingSession = make_session(tmp_path)
    monkeypatch.setattr("app.scheduler.SessionLocal", TestingSession)

    db = TestingSession()
    group = LinkedGroup(signal_group_id="group.abc", name="Lab")
    db.add(group)
    db.flush()
    schedule = Schedule(
        group_id=group.id,
        message="Hello",
        schedule_type="one_off",
        run_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        timezone="Europe/Berlin",
        enabled=True,
    )
    db.add(schedule)
    db.commit()
    schedule_id = schedule.id
    db.close()

    class FakeSignal:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, recipients, message):
            return SignalAPIResult(ok=True, status_code=201)

    monkeypatch.setattr("app.scheduler.SignalAPI", FakeSignal)

    execute_schedule(schedule_id)

    db = TestingSession()
    saved = db.get(Schedule, schedule_id)
    attempts = db.execute(select(SendAttempt)).scalars().all()
    retries = db.execute(select(RetryEntry)).scalars().all()

    assert saved.enabled is False
    assert attempts[0].status == "success"
    assert retries == []
    db.close()


def test_execute_schedule_enqueues_retry_on_failure(monkeypatch, tmp_path):
    TestingSession = make_session(tmp_path)
    monkeypatch.setattr("app.scheduler.SessionLocal", TestingSession)

    db = TestingSession()
    group = LinkedGroup(signal_group_id="group.abc", name="Lab")
    db.add(group)
    db.flush()
    schedule = Schedule(
        group_id=group.id,
        message="Hello",
        schedule_type="weekly",
        day_of_week=3,
        time_of_day="15:00",
        timezone="Europe/Berlin",
        enabled=True,
    )
    db.add(schedule)
    db.commit()
    schedule_id = schedule.id
    db.close()

    class FakeSignal:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, recipients, message):
            return SignalAPIResult(ok=False, status_code=503, error="offline")

    monkeypatch.setattr("app.scheduler.SignalAPI", FakeSignal)

    execute_schedule(schedule_id)

    db = TestingSession()
    attempts = db.execute(select(SendAttempt)).scalars().all()
    retries = db.execute(select(RetryEntry)).scalars().all()

    assert attempts[0].status == "failed"
    assert retries[0].status == "pending"
    assert retries[0].message == "Hello"
    db.close()


def test_process_retry_queue_marks_success(monkeypatch, tmp_path):
    TestingSession = make_session(tmp_path)
    monkeypatch.setattr("app.scheduler.SessionLocal", TestingSession)

    db = TestingSession()
    group = LinkedGroup(signal_group_id="group.abc", name="Lab")
    db.add(group)
    db.flush()
    retry = RetryEntry(
        group_id=group.id,
        message="Retry me",
        status="pending",
        first_failed_at=datetime.now(timezone.utc),
        last_attempt_at=datetime.now(timezone.utc),
        attempts=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(retry)
    db.commit()
    retry_id = retry.id
    db.close()

    class FakeSignal:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, recipients, message):
            return SignalAPIResult(ok=True, status_code=201)

    monkeypatch.setattr("app.scheduler.SignalAPI", FakeSignal)

    process_retry_queue()

    db = TestingSession()
    saved = db.get(RetryEntry, retry_id)
    attempts = db.execute(select(SendAttempt)).scalars().all()

    assert saved.status == "sent"
    assert saved.attempts == 2
    assert attempts[0].status == "success"
    db.close()


def test_create_scheduler_adds_signal_receive_job():
    scheduler = create_scheduler()

    assert scheduler.get_job("signal-receive") is not None


def test_process_signal_updates_passes_received_payload(monkeypatch):
    payload = [{"envelope": {"dataMessage": {"message": "!ping"}}}]
    handled = []

    class FakeSignal:
        def receive_updates(self):
            return SignalAPIResult(ok=True, data=payload)

    monkeypatch.setattr("app.scheduler.create_signal_api", lambda: FakeSignal())
    monkeypatch.setattr("app.scheduler.handle_signal_updates", lambda data: handled.append(data) or 1)

    process_signal_updates()

    assert handled == [payload]
