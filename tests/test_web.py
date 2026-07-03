from datetime import timedelta
from types import SimpleNamespace

from app import auth
from app.config import Settings
from app.database import Base
import app.main as main_module
from app.main import app, normalize_group, parse_schedule_form
from app.models import LinkedGroup, RetryEntry, Schedule, SendAttempt, utc_now
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


class FakeRequest:
    def __init__(self):
        self.session = {}
        self.app = SimpleNamespace(state=SimpleNamespace(scheduler=object()))


def test_expected_routes_are_registered():
    paths = {route.path for route in app.routes}

    assert "/" in paths
    assert "/groups" in paths
    assert "/groups/import" in paths
    assert "/groups/{group_id}/delete" in paths
    assert "/schedules" in paths
    assert "/schedules/new" in paths
    assert "/attempts" in paths
    assert "/auth/login" in paths
    assert "/auth/callback" in paths


def test_normalize_group_accepts_common_shapes():
    assert normalize_group({"id": "group.abc", "name": "Lab"}) == {
        "signal_group_id": "group.abc",
        "name": "Lab",
    }
    assert normalize_group("group.xyz") == {
        "signal_group_id": "group.xyz",
        "name": "group.xyz",
    }


def test_auth_bypass_returns_development_user(monkeypatch):
    monkeypatch.setattr(auth, "settings", Settings(auth_bypass_for_development=True))

    user = auth.current_user(FakeRequest())

    assert user["sub"] == "development-auth-bypass"
    assert user["name"] == "Development Admin"


def test_auth_bypass_disabled_requires_session(monkeypatch):
    monkeypatch.setattr(auth, "settings", Settings(auth_bypass_for_development=False))

    try:
        auth.current_user(FakeRequest())
    except Exception as exc:
        assert getattr(exc, "status_code") == 401
    else:
        raise AssertionError("current_user should reject missing sessions without auth bypass")


def test_oidc_group_restriction_uses_groups_claim():
    settings = Settings(oidc_allowed_groups="/signal-admins,/signal-operators")

    assert auth.is_authorized({"groups": ["/signal-operators"]}, settings)
    assert not auth.is_authorized({"groups": ["/other-group"]}, settings)


def test_oidc_group_restriction_supports_legacy_single_group_setting():
    settings = Settings(oidc_allowed_group="/signal-admins")

    assert auth.is_authorized({"groups": ["/signal-admins"]}, settings)


def test_oidc_group_restriction_ignores_realm_access_groups():
    settings = Settings(oidc_allowed_groups="/signal-admins")
    user = {"realm_access": {"groups": ["/signal-admins"]}}

    assert not auth.is_authorized(user, settings)


def test_template_response_passes_request_first(monkeypatch):
    captured = {}

    class FakeTemplates:
        def TemplateResponse(self, request, name, context, status_code=200):
            captured["request"] = request
            captured["name"] = name
            captured["context"] = context
            captured["status_code"] = status_code
            return "response"

    request = FakeRequest()
    monkeypatch.setattr(main_module, "templates", FakeTemplates())
    monkeypatch.setattr(main_module, "settings", Settings(auth_bypass_for_development=True))

    response = main_module.template(request, "dashboard.html", {"group_count": 0}, status_code=202)

    assert response == "response"
    assert captured["request"] is request
    assert captured["name"] == "dashboard.html"
    assert captured["status_code"] == 202
    assert captured["context"]["user"]["sub"] == "development-auth-bypass"


def test_logout_redirects_to_dashboard_by_default(monkeypatch):
    request = FakeRequest()
    request.session["user"] = {"sub": "user-1"}
    monkeypatch.setattr(main_module, "settings", Settings(logout_redirect_url=""))

    response = main_module.logout(request)

    assert request.session == {}
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_logout_uses_configured_redirect_url(monkeypatch):
    request = FakeRequest()
    request.session["user"] = {"sub": "user-1"}
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(logout_redirect_url="https://sso.example.test/logout"),
    )

    response = main_module.logout(request)

    assert request.session == {}
    assert response.status_code == 303
    assert response.headers["location"] == "https://sso.example.test/logout"


def test_blank_schedule_form_returns_friendly_errors(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, future=True)
    db = TestingSession()

    values, errors = parse_schedule_form(
        {
            "group_id": "",
            "message": "",
            "schedule_type": "weekly",
            "day_of_week": "0",
            "day_of_month": "1",
            "time_of_day": "bad",
            "timezone": "Europe/Berlin",
        },
        db,
    )

    assert values["group_id"] == 0
    assert "Choose a linked group." in errors
    assert "Message text is required." in errors
    assert "Weekly schedules need a valid time." in errors
    db.close()


def test_delete_group_removes_schedules_and_preserves_history(monkeypatch, tmp_path):
    synced = {}

    def fake_sync_schedule_jobs(scheduler):
        synced["scheduler"] = scheduler

    monkeypatch.setattr(main_module, "sync_schedule_jobs", fake_sync_schedule_jobs)
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, future=True)
    db = TestingSession()

    group = LinkedGroup(signal_group_id="group.abc", name="Lab")
    db.add(group)
    db.flush()
    schedule = Schedule(
        group_id=group.id,
        message="Hello",
        schedule_type="weekly",
        day_of_week=0,
        time_of_day="10:00",
        timezone="Europe/Berlin",
        enabled=True,
    )
    db.add(schedule)
    db.flush()
    db.add(
        SendAttempt(
            schedule_id=schedule.id,
            group_id=group.id,
            message="Hello",
            status="failed",
        )
    )
    db.add(
        RetryEntry(
            schedule_id=schedule.id,
            group_id=group.id,
            message="Hello",
            expires_at=utc_now() + timedelta(hours=1),
        )
    )
    db.commit()

    response = main_module.delete_group(group.id, FakeRequest(), db, user={})
    db.expire_all()

    assert response.status_code == 303
    assert response.headers["location"] == "/groups"
    assert db.get(LinkedGroup, group.id) is None
    assert db.execute(select(Schedule)).scalars().all() == []
    attempt = db.execute(select(SendAttempt)).scalar_one()
    retry = db.execute(select(RetryEntry)).scalar_one()
    assert attempt.group_id is None
    assert attempt.schedule_id is None
    assert retry.group_id is None
    assert retry.schedule_id is None
    assert "scheduler" in synced
    db.close()
