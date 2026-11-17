"""Prove durable claim, explicit recovery, UTC usage accounting and SQLite portability."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

import httpx
import pytest

from daily_ml.config import load_settings
from daily_ml.connection import ConnectionError
from daily_ml.database import Database
from daily_ml.grading_jobs import GradingJobs
from test_provider import catalog, provider, response_stream, terminal


def setup_jobs(tmp_path, handler, clock=lambda: 100000.0):
    """Create isolated learning storage and the real SDK with mocked I/O."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    return GradingJobs(Database(tmp_path), provider(handler), settings, clock=clock)


def test_concurrent_tabs_reuse_pending_and_terminal_result(tmp_path):
    """Both tab keys permanently point to one result and one outbound attempt."""
    calls = []
    def handle(request):
        calls.append(request)
        assert jobs.usage_count() == 1
        return response_stream(terminal())
    jobs = setup_jobs(tmp_path, handle)
    with ThreadPoolExecutor(max_workers=5) as pool:
        ids = list(pool.map(lambda k: jobs.enqueue_connection_test(k, "z")["id"], ["a", "b", "a", "c", "b"]))
    assert len(set(ids)) == 1
    async def run_workers():
        await asyncio.gather(jobs.run_pending(), jobs.run_pending())
    asyncio.run(run_workers())
    assert len(calls) == jobs.usage_count() == 1
    result = jobs.get_request(ids[0])
    assert result["state"] == "assessed" and result["grade"] == 1
    assert result["feedback"]["summary"] and result["schema_mode"] == "schema"
    assert jobs.enqueue_connection_test("b", "z") == result


def test_interrupted_attempt_survives_backup_without_automatic_replay(tmp_path):
    """Persist uncertainty before send, restore it, then require explicit retry."""
    now = [200000.0]
    def crash(_request):
        raise asyncio.CancelledError()
    jobs = setup_jobs(tmp_path / "live", crash, lambda: now[0])
    original = jobs.enqueue_connection_test("first", "z")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs.run_pending())
    backup = Database(tmp_path / "backup")
    db = Database(tmp_path / "live")
    with db.connect() as src, backup.connect() as dest:
        src.backup(dest)
    restored = Database(tmp_path / "restored")
    with backup.connect() as src, restored.connect() as dest:
        src.backup(dest)
    jobs = setup_jobs(tmp_path / "restored", lambda _: response_stream(terminal()), lambda: now[0])
    jobs.recover_interrupted()
    asyncio.run(jobs.run_pending())
    assert jobs.get_request(original["id"])["state"] == "outcome_unknown" and jobs.usage_count() == 1
    now[0] += 86400
    assert jobs.usage_count() == 0
    retry = jobs.enqueue_connection_test("second", "z", retry_of=original["id"])
    asyncio.run(jobs.run_pending())
    assert retry["id"] != original["id"] and jobs.get_request(retry["id"])["state"] == "assessed"
    assert jobs.usage_count() == 1


def test_explicit_json_retry_audited_and_access_reads_excluded(tmp_path):
    """Record one reservation per SDK call and preserve the parent's schema rejection."""
    calls = []
    def handle(request):
        import json
        body = json.loads(request.content)
        calls.append(body.get("text", {}).get("format", {}).get("type"))
        if "text" in body:
            return httpx.Response(400, json={"error": {"code": "unsupported_parameter", "param": "text.format",
                                                       "message": "private details"}})
        return response_stream(terminal())
    jobs = setup_jobs(tmp_path, handle)
    unavailable = jobs.enqueue_connection_test("bad", "unlisted")
    asyncio.run(jobs.run_pending())
    assert jobs.get_request(unavailable["id"])["error"] == "model_unavailable" and jobs.usage_count() == 0
    key = jobs.enqueue_connection_test("first", "z")["id"]
    asyncio.run(jobs.run_pending())
    assert jobs.get_request(key)["error"] == "schema_unsupported" and jobs.usage_count() == 1
    retry = jobs.enqueue_connection_test("new", "z", retry_of=key)
    asyncio.run(jobs.run_pending())
    result = jobs.get_request(retry["id"])
    assert result["schema_mode"] == "json" and result["state"] == "assessed" and jobs.usage_count() == 2
    assert calls == ["json_schema", None]


def test_queued_restart_is_recoverable_and_unsent(tmp_path):
    """Explicit recovery is required after shutdown even before any reservation."""
    jobs = setup_jobs(tmp_path, lambda _: pytest.fail("unexpected network"))
    key = jobs.enqueue_connection_test("before-shutdown", "z")["id"]
    jobs.recover_interrupted()
    asyncio.run(jobs.run_pending())
    assert jobs.get_request(key)["error"] == "unsent" and jobs.usage_count() == 0
    assert jobs.get_request("absent") is None


def test_dispatch_requires_current_authorization_after_reservation(tmp_path):
    """Admission under the credential lock must succeed before transport or billing."""
    jobs = setup_jobs(tmp_path, lambda _: pytest.fail("Transport requires current authorization"))
    job = jobs.enqueue_connection_test("authorized-click", "z")

    def current_credential():
        if jobs.usage_count():
            raise ConnectionError("Signed out")
        return "fixture-token"

    jobs.provider.connection.get_access_token = current_credential
    asyncio.run(jobs.run_pending())
    assert jobs.get_request(job["id"])["state"] == "awaiting_access"
    assert jobs.usage_count() == 0


@pytest.mark.parametrize("stage", ["claim", "finalization"])
def test_temporary_write_lock_recovers_without_another_model_call(tmp_path, monkeypatch, stage):
    """The worker owns its local transaction until the other writer releases SQLite."""
    from threading import Event, Thread

    monkeypatch.setattr("daily_ml.database.SQLITE_WAIT_SECONDS", 0.01)
    acquired = Event()
    release = Event()
    calls = []

    def hold_lock():
        with jobs.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            acquired.set()
            release.wait(timeout=5)

    def lock_temporarily():
        writer.start()
        assert acquired.wait(timeout=2)
        asyncio.get_running_loop().call_later(0.15, release.set)

    def respond(request):
        calls.append(request.url.path)
        if stage == "finalization":
            lock_temporarily()
        return response_stream(terminal())

    jobs = setup_jobs(tmp_path, respond)
    key = jobs.enqueue_connection_test("first", "z")["id"]
    writer = Thread(target=hold_lock)

    async def run():
        if stage == "claim":
            lock_temporarily()
        await asyncio.wait_for(jobs.run_pending(), timeout=3)

    try:
        asyncio.run(run())
    finally:
        release.set()
        writer.join(timeout=2)
    assert jobs.get_request(key)["state"] == "assessed"
    assert len(calls) == jobs.usage_count() == 1


def test_result_and_retry_keep_saved_reference_after_fixture_update(tmp_path, monkeypatch):
    """A new content release cannot change the question or sources used by an old retry."""
    from daily_ml.grading import GradingContext

    jobs = setup_jobs(tmp_path, lambda _: response_stream(terminal("incomplete")))
    parent = jobs.enqueue_connection_test("original", "z")
    asyncio.run(jobs.run_pending())
    original = GradingContext.connection_test()
    newer = original.model_copy(update={"question": "New question", "sources": {}})
    monkeypatch.setattr(GradingContext, "connection_test", lambda: newer)
    retry = jobs.enqueue_connection_test("retry", "z", retry_of=parent["id"])
    for job in (jobs.get_request(parent["id"]), retry):
        assert job["reference"]["question"] == original.question
        assert job["reference"]["sources"] == original.sources


@pytest.mark.parametrize("outcome", ["release", "deadline", "cancel"])
def test_reservation_contention_keeps_loop_responsive_and_counts_only_sends(tmp_path, monkeypatch, outcome):
    """A writer busy during reservation cannot starve the loop or enable a late send."""
    monkeypatch.setattr("daily_ml.database.SQLITE_WAIT_SECONDS", 0.6)
    jobs = setup_jobs(tmp_path, lambda _: pytest.fail("unexpected transport"))
    jobs.provider.timeout = 0.12 if outcome == "deadline" else 2
    key = jobs.enqueue_connection_test("single-click", "z")["id"]
    sends = []
    heartbeat = []

    async def run():
        loop = asyncio.get_running_loop()
        with closing(sqlite3.connect(jobs.database.path)) as other_writer:
            def route(request):
                if request.url.path.endswith("/models"):
                    other_writer.execute("BEGIN IMMEDIATE")
                    started = loop.time()
                    loop.call_later(0.02, lambda: heartbeat.append(loop.time() - started))
                    if outcome == "cancel":
                        loop.call_later(0.06, worker.cancel)
                    loop.call_later(0.25, other_writer.rollback)
                    return httpx.Response(200, json=catalog())
                sends.append(request.url.path)
                return response_stream(terminal())

            jobs.provider.transport = httpx.MockTransport(route)
            worker = asyncio.create_task(jobs.run_pending())
            if outcome == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await worker
            else:
                await worker

    asyncio.run(run())
    assert len(heartbeat) == 1 and heartbeat[0] < 0.45
    expected_attempts = int(outcome == "release")
    assert jobs.usage_count() == len(sends) == expected_attempts
    assert jobs.get_request(key)["state"] == ("assessed" if expected_attempts else "failed_recoverable")


@pytest.mark.parametrize("reservation_committed", [False, True])
def test_cancelled_reservation_cannot_reopen_terminal_job(tmp_path, monkeypatch, reservation_committed):
    """Both SQLite transaction orderings leave a cancelled, unadmitted request uncounted."""
    from threading import Event

    jobs = setup_jobs(tmp_path, lambda _: pytest.fail("Cancelled request reached transport"))
    key = jobs.enqueue_connection_test("cancelled-click", "z")["id"]
    reserve = jobs._reserve
    release_thread = Event()

    async def run():
        loop = asyncio.get_running_loop()
        thread_parked = asyncio.Event()
        thread_exited = asyncio.Event()

        def pause_reservation(request_id):
            try:
                if reservation_committed:
                    reserve(request_id)
                loop.call_soon_threadsafe(thread_parked.set)
                if not release_thread.wait(timeout=4):
                    raise AssertionError("Reservation thread was not released")
                if not reservation_committed:
                    reserve(request_id)
            finally:
                loop.call_soon_threadsafe(thread_exited.set)

        monkeypatch.setattr(jobs, "_reserve", pause_reservation)
        worker = asyncio.create_task(jobs.run_pending())
        try:
            await asyncio.wait_for(thread_parked.wait(), timeout=2)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=2)
            assert jobs.usage_count() == 0
            assert jobs.get_request(key)["state"] == "failed_recoverable"
        finally:
            release_thread.set()
        await asyncio.wait_for(thread_exited.wait(), timeout=2)

    asyncio.run(run())
    assert jobs.usage_count() == 0
    assert jobs.get_request(key)["state"] == "failed_recoverable"
