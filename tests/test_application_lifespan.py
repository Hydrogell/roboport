import asyncio
import threading
import time

from fastapi.testclient import TestClient

from app import db, lifespan, main


def test_lifespan_leaves_recent_update_for_independent_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    now = int(time.time())
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO panel_updates (started_at, source, status) VALUES (?, 'upload', 'running')",
            [(now - 3600,), (now - 60,)],
        )

    async def exercise():
        for _ in range(2):
            async with main.app.router.lifespan_context(main.app):
                state = main.app.state
                workers = (state.sampler_task, state.schedule_task, state.webhook_dispatcher)
                assert all(not task.done() for task in workers)
                with db.connect() as conn:
                    statuses = conn.execute("SELECT status FROM panel_updates ORDER BY id").fetchall()
                assert [row["status"] for row in statuses] == ["failed", "running"]
                await asyncio.sleep(0)
            assert all(task.done() for task in workers)

    asyncio.run(exercise())


def test_shutdown_prevents_subsequent_autostarts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO servers (uuid, name, version, port, autostart) VALUES (?, ?, '2.0', ?, ?)",
            [("manual", "Manual", 34197, 0), ("one", "One", 34198, 1), ("two", "Two", 34199, 1)],
        )
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Runtime:
        def start(self, spec):
            calls.append(spec.uuid)
            started.set()
            release.wait(5)

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(lifespan, "get_runtime", lambda _: Runtime())
    monkeypatch.setattr(lifespan, "sampler_loop", idle)
    monkeypatch.setattr(lifespan, "schedule_loop", idle)

    async def exercise():
        try:
            async with main.app.router.lifespan_context(main.app):
                assert await asyncio.to_thread(started.wait, 3)
        finally:
            release.set()

    asyncio.run(exercise())
    assert len(calls) == 1 and calls[0] in {"one", "two"}


def test_cached_setup_template_uses_each_requests_language_and_nonce(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    client = TestClient(main.app)
    first = client.get("/setup", headers={"Accept-Language": "en"})
    second = client.get("/setup", headers={"Accept-Language": "ru"})

    assert first.status_code == second.status_code == 200
    assert first.template is second.template
    assert first.context["locale_code"] == "en" and second.context["locale_code"] == "ru"
    first_nonce, second_nonce = first.context["csp_nonce"], second.context["csp_nonce"]
    assert first_nonce and second_nonce and first_nonce != second_nonce
    assert f'nonce="{first_nonce}"' in first.text
    assert f'nonce="{second_nonce}"' in second.text
