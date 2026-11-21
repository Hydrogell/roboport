import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import db


@pytest.fixture
def schedule_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()


def test_parse_schedule_accepts_supported_units_and_rejects_invalid_values():
    from app.schedules import parse_schedule

    assert parse_schedule("every:30m") == {
        "type": "every", "interval_sec": 1800, "raw": "every:30m",
    }
    assert parse_schedule("every:2h")["interval_sec"] == 7200
    assert parse_schedule("daily:3:05") == {
        "type": "daily", "hour": 3, "minute": 5, "raw": "daily:3:05",
    }
    assert parse_schedule("weekly:mon,fri:03:30") == {
        "type": "weekly", "dows": {0, 4}, "hour": 3, "minute": 30,
        "raw": "weekly:mon,fri:03:30",
    }
    for invalid in ("every:0m", "every:2d", "daily:24:00", "weekly:mo:03:30"):
        assert parse_schedule(invalid) is None


def test_should_fire_keeps_daily_cutoff_in_panel_timezone(monkeypatch):
    from app import schedules

    tz = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(schedules, "panel_tz", lambda: tz)
    target = int(datetime(2026, 11, 20, 3, 30, tzinfo=tz).timestamp())
    parsed = schedules.parse_schedule("daily:03:30")

    assert schedules.should_fire(parsed, None, target - 1) is False
    assert schedules.should_fire(parsed, None, target) is True
    assert schedules.should_fire(parsed, target, target + 60) is False


def test_should_fire_handles_initial_intervals_and_weekly_day_gate(monkeypatch):
    from app import schedules

    tz = timezone(timedelta(hours=-8))
    monkeypatch.setattr(schedules, "panel_tz", lambda: tz)
    now = int(datetime(2026, 11, 23, 3, 30, tzinfo=tz).timestamp())  # Monday

    assert schedules.should_fire(schedules.parse_schedule("every:2h"), None, now)
    assert not schedules.should_fire(schedules.parse_schedule("every:2h"), now - 7199, now)
    assert schedules.should_fire(schedules.parse_schedule("every:2h"), now - 7200, now)
    assert schedules.should_fire(schedules.parse_schedule("weekly:mon:03:30"), None, now)
    assert not schedules.should_fire(schedules.parse_schedule("weekly:tue:03:30"), None, now)


def test_task_queries_preserve_task_order_and_scoped_mutations(schedule_db):
    from app.schedules import (
        create_task, delete_task, due_tasks, get_task, list_tasks, record_task_run,
        update_task,
    )

    with db.connect() as conn:
        for uuid, port in (("first", 34197), ("second", 34198)):
            conn.execute(
                "INSERT INTO servers (uuid, name, version, port) VALUES (?, ?, ?, ?)",
                (uuid, uuid, "2.0", port),
            )
        first_id = create_task(conn, "first", "command", "every:30m", "/server-save", 0)
        second_id = create_task(conn, "first", "restart", "daily:03:30", None, 60)
        foreign_id = create_task(conn, "second", "command", "every:1h", "/players", 0)

        assert [row["id"] for row in list_tasks(conn, "first")] == [first_id, second_id]
        assert get_task(conn, second_id, "first")["warn_seconds"] == 60
        assert get_task(conn, foreign_id, "first") is None
        assert [row["id"] for row in due_tasks(conn)] == [first_id, second_id, foreign_id]

        assert update_task(conn, first_id, "first", {"enabled": 0}) == 1
        assert update_task(conn, foreign_id, "first", {"enabled": 0}) == 0
        assert delete_task(conn, foreign_id, "first") == 0
        assert delete_task(conn, foreign_id, "second") == 1
        record_task_run(conn, second_id, 123, "ok")
        assert get_task(conn, second_id, "first")["last_status"] == "ok"


def test_web_and_api_schedule_routes_use_scoped_sql_operations(schedule_db):
    from app import auth
    from app.routes.api.schedules import (
        ScheduleCreateRequest, ScheduleUpdateRequest, api_schedule_create,
        api_schedule_delete, api_schedule_list, api_schedule_update,
    )
    from app.routes.server_schedule import schedule_toggle

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO servers (uuid, name, version, port) VALUES (?, ?, ?, ?)",
            ("first", "First", "2.0", 34197),
        )
        conn.execute(
            "INSERT INTO servers (uuid, name, version, port) VALUES (?, ?, ?, ?)",
            ("second", "Second", "2.0", 34198),
        )
        user_id = auth.create_user(conn, "schedule-admin", "correct horse battery staple")
        key = auth.ApiKeyInfo(1, "route-test", {"read", "control", "command"}, user_id, 60, None)

        created = api_schedule_create(
            "first",
            ScheduleCreateRequest(kind="command", schedule="every:30m", command=" /server-save "),
            conn,
            key,
        )
        task_id = created["id"]
        assert created["command"] == "/server-save"
        assert api_schedule_list("first", conn, key)["tasks"] == [created]

        updated = api_schedule_update(
            "first", task_id, ScheduleUpdateRequest(enabled=False, warn_seconds=42), conn, key,
        )
        assert updated["enabled"] is False
        # API intentionally permits warn_seconds for command tasks on PATCH.
        assert updated["warn_seconds"] == 42

        response = schedule_toggle("first", task_id, "1", conn)
        assert response.status_code == 303
        assert conn.execute(
            "SELECT enabled FROM scheduled_tasks WHERE id = ?", (task_id,),
        ).fetchone()["enabled"] == 1

        foreign = conn.execute(
            "INSERT INTO scheduled_tasks (server_uuid, kind, schedule) VALUES (?, ?, ?)",
            ("second", "restart", "daily:03:30"),
        ).lastrowid
        deleted = api_schedule_delete("first", task_id, conn, key)
        assert deleted == {"deleted": True, "id": task_id}
        assert conn.execute("SELECT 1 FROM scheduled_tasks WHERE id = ?", (foreign,)).fetchone()


def test_api_schedule_command_actions_require_command_scope(schedule_db):
    """A control-only key cannot create or change a task that may run RCON."""
    from app import auth
    from app.routes.api.auth import ApiAuthError
    from app.routes.api.schedules import (
        ScheduleCreateRequest, ScheduleUpdateRequest, api_schedule_create,
        api_schedule_update,
    )

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO servers (uuid, name, version, port) VALUES (?, ?, ?, ?)",
            ("first", "First", "2.0", 34197),
        )
        user_id = auth.create_user(conn, "schedule-policy", "correct horse battery staple")
        control_key = auth.ApiKeyInfo(2, "control-only", {"control"}, user_id, 60, None)

        with pytest.raises(ApiAuthError) as denied_create:
            api_schedule_create(
                "first",
                ScheduleCreateRequest(kind="command", schedule="every:30m", command="/server-save"),
                conn,
                control_key,
            )
        assert denied_create.value.status_code == 403

        restart = api_schedule_create(
            "first",
            ScheduleCreateRequest(kind="restart", schedule="every:30m"),
            conn,
            control_key,
        )
        assert restart["kind"] == "restart"
        restart_after_irrelevant_command = api_schedule_update(
            "first", restart["id"], ScheduleUpdateRequest(command="/ignored"), conn, control_key,
        )
        assert restart_after_irrelevant_command["command"] is None

        command_task = conn.execute(
            "INSERT INTO scheduled_tasks (server_uuid, kind, schedule, command, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("first", "command", "every:30m", "/server-save", 0),
        ).lastrowid
        with pytest.raises(ApiAuthError) as denied_enable:
            api_schedule_update(
                "first", command_task, ScheduleUpdateRequest(enabled=True), conn, control_key,
            )
        assert denied_enable.value.status_code == 403


def test_schedule_shutdown_during_warning_leaves_restart_due(schedule_db, monkeypatch):
    from app import schedule_runner

    with db.connect() as conn:
        conn.execute("INSERT INTO servers (uuid, name, version, port) VALUES ('srv', 'Srv', '2.0', 34197)")
        task_id = conn.execute(
            "INSERT INTO scheduled_tasks (server_uuid, kind, schedule, warn_seconds) VALUES ('srv', 'restart', 'every:1m', 60)"
        ).lastrowid

    class Runtime:
        restarted = False
        def send_command(self, *_args): pass
        def restart(self, *_args): self.restarted = True

    runtime = Runtime()
    monkeypatch.setattr(schedule_runner, "DockerRuntime", lambda: runtime)
    monkeypatch.setattr(schedule_runner, "_inspect", lambda _name: {"State": {"Status": "running"}})

    class StopOnWait(threading.Event):
        def wait(self, _timeout=None):
            self.set()
            return True

    schedule_runner._tick_schedule(100, StopOnWait())
    with db.connect() as conn:
        row = conn.execute("SELECT last_run, last_status FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["last_run"] is None and row["last_status"] is None
    assert runtime.restarted is False


def test_schedule_drains_started_command_and_records_once_before_shutdown(schedule_db, monkeypatch):
    from app import schedule_runner

    with db.connect() as conn:
        conn.execute("INSERT INTO servers (uuid, name, version, port) VALUES ('srv', 'Srv', '2.0', 34197)")
        first = conn.execute(
            "INSERT INTO scheduled_tasks (server_uuid, kind, schedule, command) VALUES ('srv', 'command', 'every:1m', '/one')"
        ).lastrowid
        second = conn.execute(
            "INSERT INTO scheduled_tasks (server_uuid, kind, schedule, command) VALUES ('srv', 'command', 'every:1m', '/two')"
        ).lastrowid

    started, release, stop = threading.Event(), threading.Event(), threading.Event()
    class Runtime:
        def send_command(self, _spec, _command):
            started.set()
            assert release.wait(8)
        def restart(self, *_args): raise AssertionError("not expected")

    fired = []
    monkeypatch.setattr(schedule_runner, "DockerRuntime", Runtime)
    monkeypatch.setattr(schedule_runner.time, "time", lambda: 100)
    monkeypatch.setattr(schedule_runner, "_inspect", lambda _name: {"State": {"Status": "running"}})
    monkeypatch.setattr(schedule_runner.webhooks, "emit", lambda *args, **kwargs: fired.append((args, kwargs)))
    worker = threading.Thread(target=schedule_runner._tick_schedule, args=(100, stop))
    worker.start()
    assert started.wait(5)
    stop.set()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    with db.connect() as conn:
        rows = conn.execute("SELECT id, last_run, last_status FROM scheduled_tasks ORDER BY id").fetchall()
    assert (rows[0]["id"], rows[0]["last_run"], rows[0]["last_status"]) == (first, 100, "ok")
    assert rows[1]["id"] == second and rows[1]["last_run"] is None
    assert len(fired) == 1 and fired[0][1]["task_id"] == first


def test_long_restart_waits_a_full_interval_before_firing_again(schedule_db, monkeypatch):
    from app import schedule_runner

    with db.connect() as conn:
        conn.execute("INSERT INTO servers (uuid,name,version,port) VALUES ('srv','Srv','2.0',34197)")
        conn.execute("INSERT INTO scheduled_tasks (server_uuid,kind,schedule,warn_seconds) "
                     "VALUES ('srv','restart','every:1m',600)")
    clock = [100]
    warnings, restarts = [], []

    class AdvanceClock(threading.Event):
        def wait(self, seconds):
            clock[0] += seconds
            return False

    class Runtime:
        def send_command(self, spec, message):
            warnings.append(message)

        def restart(self, spec):
            restarts.append(clock[0])

    monkeypatch.setattr(schedule_runner, "DockerRuntime", Runtime)
    monkeypatch.setattr(schedule_runner, "_inspect", lambda _: {"State": {"Status": "running"}})
    monkeypatch.setattr(schedule_runner.time, "time", lambda: clock[0])
    monkeypatch.setattr(schedule_runner.webhooks, "emit", lambda *args, **kwargs: None)
    stop = AdvanceClock()
    schedule_runner._tick_schedule(clock[0], stop)
    clock[0] += 30
    schedule_runner._tick_schedule(clock[0], stop)

    assert restarts == [700]
    assert warnings == [f"/server-message Перезапуск через {n} сек" for n in (600, 30, 10, 5)]
    with db.connect() as conn:
        assert conn.execute("SELECT last_run FROM scheduled_tasks").fetchone()[0] == 700


def test_metrics_keep_sampling_while_a_scheduled_command_is_running(schedule_db, monkeypatch):
    from app import main, sampler, schedule_runner

    with db.connect() as conn:
        conn.execute("INSERT INTO servers (uuid,name,version,port) VALUES ('srv','Srv','2.0',34197)")
        conn.execute("INSERT INTO scheduled_tasks (server_uuid,kind,schedule,command) "
                     "VALUES ('srv','command','every:1h','/server-save')")
    command_started = threading.Event()
    release_command = threading.Event()

    class Runtime:
        def send_command(self, spec, command):
            command_started.set()
            assert release_command.wait(10)

    monkeypatch.setattr(schedule_runner, "DockerRuntime", Runtime)
    monkeypatch.setattr(schedule_runner, "_inspect", lambda _: {"State": {"Status": "running"}})
    monkeypatch.setattr(sampler, "SAMPLE_INTERVAL", 0.01)

    async def exercise():
        samples = []
        sampled_twice = asyncio.Event()

        async def sample(row, timestamp):
            if command_started.is_set():
                samples.append(row["uuid"])
                if len(samples) == 2:
                    sampled_twice.set()

        monkeypatch.setattr(sampler, "_tick_one", sample)
        async with main.app.router.lifespan_context(main.app):
            try:
                await asyncio.wait_for(sampled_twice.wait(), timeout=5)
                assert samples[:2] == ["srv", "srv"]
            finally:
                release_command.set()

    asyncio.run(exercise())
