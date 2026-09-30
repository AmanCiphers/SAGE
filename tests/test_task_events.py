"""Per-task timeline, so a background goal reports what it is actually doing."""

import threading
import time

import pytest

from sage.tasks import TaskManager, TaskStore, WorkerLimit


@pytest.fixture
def store(tmp_path):
    return TaskStore(str(tmp_path / "events.db"))


def stream_of(*events):
    """A stream runner that replays fixed events, as the pipeline would."""
    def runner(goal):
        for event in events:
            yield event

    return runner


def done(response="the answer", status="completed"):
    return {"done": True, "response": response, "status": status, "job": object()}


def wait_for(store, task_id, statuses, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store.get_task(task_id) or {}
        if row.get("status") in statuses:
            return row
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {statuses}: {store.get_task(task_id)}")


class TestSchema:
    def test_a_fresh_store_has_the_worker_columns(self, store):
        columns = store.event_columns()
        for name in ("stage", "error", "summary", "conversation_id",
                     "cancel_requested", "started_at", "finished_at",
                     "surface", "local_tools"):
            assert name in columns, f"{name} missing from tasks"

    def test_the_event_table_exists(self, store):
        assert store.event_columns() is not None

    def test_an_existing_table_is_migrated_in_place(self, tmp_path):
        """A database written by the old schema has to gain the columns.

        The live tasks table was created by a previous version, so
        CREATE TABLE IF NOT EXISTS alone would leave it without them and every
        worker write would fail.
        """
        import sqlite3

        path = tmp_path / "old.db"
        legacy = sqlite3.connect(path)
        legacy.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, "
            "created_at INTEGER, updated_at INTEGER, progress TEXT, result TEXT)"
        )
        legacy.execute(
            "INSERT INTO tasks VALUES ('a1','old goal','done',1,1,'finished','kept')"
        )
        legacy.commit()
        legacy.close()

        store = TaskStore(str(path))

        assert "stage" in store.event_columns()
        # Migration must not disturb what was already there.
        row = store.get_task("a1")
        assert row["title"] == "old goal"
        assert row["result"] == "kept"

    def test_migration_is_idempotent(self, tmp_path):
        path = str(tmp_path / "twice.db")
        TaskStore(path).initialize()
        TaskStore(path).initialize()  # must not raise

    def test_the_shared_path_env_var_still_applies(self, tmp_path, monkeypatch):
        target = tmp_path / "env.db"
        monkeypatch.setenv("SAGE_DB_PATH", str(target))
        assert TaskStore().path == str(target)


class TestTimelineIsObservedNotInvented:
    """Progress is what the worker did, recorded as it happened.

    The alternative was a fraction derived from elapsed time, which is a
    statement about the clock, not about the work.
    """

    def test_stage_events_are_recorded_in_order(self, store):
        manager = TaskManager(store, stream_runner=stream_of(
            {"stage": "analyzing"},
            {"stage": "routing"},
            {"stage": "tool", "name": "bash", "arguments": {}},
            done(),
        ))
        task_id = manager.start("walk the stages")
        wait_for(store, task_id, ("done",))

        events = store.events(task_id)
        assert [e["stage"] for e in events] == ["analyzing", "routing", "tool"]
        assert [e["seq"] for e in events] == [1, 2, 3]

    def test_a_tool_call_records_whether_it_worked(self, store):
        manager = TaskManager(store, stream_runner=stream_of(
            {"tool": "bash", "ok": True, "summary": "3 files"},
            {"tool": "web_search", "ok": False, "summary": "timed out"},
            done(),
        ))
        task_id = manager.start("call some tools")
        wait_for(store, task_id, ("done",))

        tools = [e for e in store.events(task_id) if e["kind"] == "tool"]
        assert [(t["name"], t["ok"]) for t in tools] == [
            ("bash", 1), ("web_search", 0)
        ]

    def test_the_stage_column_tracks_the_current_stage(self, store):
        manager = TaskManager(store, stream_runner=stream_of(
            {"stage": "analyzing"}, {"stage": "retrieving", "tools": ["bash"]},
            done(),
        ))
        task_id = manager.start("stage column")
        wait_for(store, task_id, ("done",))

        assert store.events(task_id)[-1]["detail"] == "bash"

    def test_events_can_be_read_from_a_sequence(self, store):
        manager = TaskManager(store, stream_runner=stream_of(
            {"stage": "analyzing"}, {"stage": "routing"},
            {"stage": "tool", "name": "bash"}, done(),
        ))
        task_id = manager.start("tail the stream")
        wait_for(store, task_id, ("done",))

        # The client reconnects mid-run and asks for what it has not seen.
        assert len(store.events(task_id)) == 3
        assert [e["seq"] for e in store.events(task_id, after_seq=2)] == [3]

    def test_the_full_streamed_result_is_stored(self, store):
        long_answer = "z" * 5000
        manager = TaskManager(store, stream_runner=stream_of(done(long_answer)))
        task_id = manager.start("long streamed answer")
        wait_for(store, task_id, ("done",))

        assert store.get_task(task_id)["result"] == long_answer


class TestErrorIsNotAnAnswer:
    def test_a_failure_keeps_result_empty(self, store):
        """An error used to be written into result, so a failed task looked
        like one that had produced text."""
        def runner(goal):
            yield {"error": "RuntimeError: model unavailable"}
            yield done("", "failed")

        manager = TaskManager(store, stream_runner=runner)
        task_id = manager.start("fails")
        row = wait_for(store, task_id, ("failed",))

        assert row["error"] == "RuntimeError: model unavailable"
        assert not row["result"]

    def test_a_stream_with_no_final_event_fails_honestly(self, store):
        manager = TaskManager(store, stream_runner=stream_of({"stage": "analyzing"}))
        task_id = manager.start("ends early")
        row = wait_for(store, task_id, ("failed",))

        assert "without a result" in row["error"]

    def test_a_raised_exception_is_recorded_as_a_failure(self, store):
        def runner(goal):
            raise RuntimeError("no model")
            yield  # pragma: no cover - generator marker

        manager = TaskManager(store, stream_runner=runner)
        task_id = manager.start("explodes")
        row = wait_for(store, task_id, ("failed",))

        assert row["error"].startswith("RuntimeError")
        assert not row["result"]


class TestWorkerParksInsteadOfApprovingItself:
    def test_an_approval_event_stops_the_worker(self, store):
        def runner(goal):
            yield {"stage": "tool", "name": "bash", "arguments": {}}
            yield {"approval": {"id": "ap1", "description": "rm -rf /tmp/x"}}
            yield done("this should never be reached", "completed")

        manager = TaskManager(store, stream_runner=runner)
        task_id = manager.start("does something destructive")
        row = wait_for(store, task_id, ("needs_approval",))

        # Nothing ran, so there is no answer to show.
        assert not row["result"]
        assert "approval" in row["error"]

        parked = [e for e in store.events(task_id) if e["kind"] == "approval"]
        assert parked and parked[0]["name"] == "ap1"


class TestCancel:
    def test_cancel_stops_a_running_worker(self, store):
        running = threading.Event()

        def runner(goal):
            yield {"stage": "analyzing"}
            running.set()
            for _ in range(300):
                time.sleep(0.01)
                yield {"stage": "tool", "name": "bash"}
            yield done("finished anyway")  # pragma: no cover

        manager = TaskManager(store, stream_runner=runner)
        task_id = manager.start("a long job")
        assert running.wait(3)

        assert manager.cancel(task_id) is True
        row = wait_for(store, task_id, ("cancelled",))

        assert not row["result"]

    def test_cancelling_a_finished_task_reports_false(self, store):
        manager = TaskManager(store, stream_runner=stream_of(done()))
        task_id = manager.start("quick")
        wait_for(store, task_id, ("done",))

        assert manager.cancel(task_id) is False

    def test_cancelling_an_unknown_task_reports_false(self, store):
        manager = TaskManager(store, stream_runner=stream_of(done()))
        assert manager.cancel("nope") is False


class TestWorkerLimit:
    def test_the_cap_refuses_a_further_worker(self, store):
        holding = threading.Event()
        release = threading.Event()

        def runner(goal):
            yield {"stage": "analyzing"}
            holding.set()
            release.wait(3)
            yield done()

        manager = TaskManager(store, stream_runner=runner, max_workers=2)
        manager.start("one")
        manager.start("two")
        assert holding.wait(3)

        with pytest.raises(WorkerLimit):
            manager.start("three")

        release.set()
        wait_for(store, manager.store.list_tasks()[0]["id"], ("done",))

    def test_a_finished_worker_frees_its_slot(self, store):
        manager = TaskManager(store, stream_runner=stream_of(done()), max_workers=1)
        first = manager.start("one")
        wait_for(store, first, ("done",))
        time.sleep(0.05)

        assert manager.at_capacity() is False
        second = manager.start("two")
        wait_for(store, second, ("done",))

    def test_at_capacity_reports_the_running_count(self, store):
        holding = threading.Event()
        release = threading.Event()

        def runner(goal):
            holding.set()
            release.wait(3)
            yield done()

        manager = TaskManager(store, stream_runner=runner, max_workers=3)
        assert manager.at_capacity() is False

        manager.start("one")
        assert holding.wait(3)
        assert manager.at_capacity() is False  # one of three

        release.set()


class TestCompletionReachesTheUser:
    """The on_done hook existed but nothing was ever wired to it, so a task
    could finish in the background and the user would never be told."""

    def test_on_complete_fires_for_any_task(self, store):
        seen = []
        manager = TaskManager(
            store, stream_runner=stream_of(done("finished quietly")),
            on_complete=lambda row, status: seen.append((row["id"], status)),
        )
        task_id = manager.start("a job")
        wait_for(store, task_id, ("done",))

        for _ in range(100):
            if seen:
                break
            time.sleep(0.02)

        assert seen == [(task_id, "done")]

    def test_a_broken_notifier_does_not_fail_the_task(self, store):
        def explode(row, status):
            raise RuntimeError("client gone")

        manager = TaskManager(store, stream_runner=stream_of(done("all good")),
                              on_complete=explode)
        task_id = manager.start("a job")
        row = wait_for(store, task_id, ("done",))

        # The work is already stored; a failed notice must not undo it.
        assert row["result"] == "all good"
        assert row["status"] == "done"

    def test_the_notice_is_a_pointer_not_the_answer(self, store):
        from sage.web.api import _task_finished

        seen = {}

        class FakeDB:
            def add_notification(self, kind, body):
                seen["kind"] = kind
                seen["body"] = body

        import sage.web.api as api_module
        original = api_module.db
        api_module.db = FakeDB()
        try:
            _task_finished(
                {"id": "abc123", "title": "repo breakdown", "result": "x" * 4000},
                "done",
            )
        finally:
            api_module.db = original

        assert seen["kind"] == "task_done"
        assert "abc123" in seen["body"]
        assert "repo breakdown" in seen["body"]
        # A notification is not a place for a whole answer.
        assert "x" * 100 not in seen["body"]

    def test_a_failed_task_is_reported_as_such(self, store):
        from sage.web.api import _task_finished

        seen = {}

        class FakeDB:
            def add_notification(self, kind, body):
                seen["body"] = body

        import sage.web.api as api_module
        original = api_module.db
        api_module.db = FakeDB()
        try:
            _task_finished({"id": "z9", "title": "the job"}, "failed")
        finally:
            api_module.db = original

        assert "Could not finish" in seen["body"]


class TestStreamRunnerInheritsTheSurface:
    def test_the_streaming_runner_does_not_hardcode_cli(self, store, monkeypatch):
        import sage.core.orchestrator as orchestrator_module
        from sage.tasks import _default_stream_runner

        captured = {}

        class FakeOrchestrator:
            def __init__(self, surface="cli", local_tools=True, store=None,
                         conversation_id=None):
                captured["surface"] = surface
                captured["local_tools"] = local_tools

            def run_stream(self, goal, surface="cli"):
                captured["run_surface"] = surface
                yield done()

        monkeypatch.setattr(orchestrator_module, "Orchestrator", FakeOrchestrator)

        manager = TaskManager(store, stream_runner=_default_stream_runner)
        task_id = manager.start("web work", surface="web", local_tools=False)
        wait_for(store, task_id, ("done",))

        assert captured["surface"] == "web"
        assert captured["local_tools"] is False
        assert captured["run_surface"] == "web"
