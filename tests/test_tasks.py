"""Task store, scheduler, and loop semantics."""

import time

import pytest

from sage.tasks import (
    Scheduler,
    TaskManager,
    TaskStore,
    current_task_context,
    get_runtime,
)


@pytest.fixture
def store(tmp_path):
    return TaskStore(str(tmp_path / "tasks.db"))


@pytest.fixture
def manager(store):
    return TaskManager(store, runner=lambda goal, report: f"done:{goal}")


class TestTaskStore:
    def test_lifecycle(self, store):
        task_id = store.create_task("count the stars")
        assert store.get_task(task_id)["status"] == "queued"

        store.update_task(task_id, status="running", progress="halfway")
        row = store.get_task(task_id)
        assert row["status"] == "running"
        assert row["progress"] == "halfway"

        store.update_task(task_id, result="42")
        assert store.get_task(task_id)["result"] == "42"

    def test_update_ignores_unknown_fields(self, store):
        task_id = store.create_task("x")
        store.update_task(task_id, status="done", bogus="nope")
        assert "bogus" not in store.get_task(task_id)

    def test_missing_task(self, store):
        assert store.get_task("nope") is None

    def test_list_tasks_newest_first(self, store):
        store.create_task("first")
        time.sleep(0.01)
        store.create_task("second")
        assert store.list_tasks()[0]["title"] == "second"


class TestReminders:
    def test_one_off_fires_then_is_marked_done(self, store):
        store.add_reminder("stand up", due_in_seconds=-1)

        assert store.due_reminders()

        store.mark_done(store.due_reminders()[0]["id"])
        assert store.due_reminders() == []

    def test_recurring_reminder_requeues(self, store):
        reminder_id = store.add_reminder("[loop] poll", due_in_seconds=-1, repeat_interval=60)
        store.requeue(reminder_id, 60)

        assert store.due_reminders() == []

    def test_cancel_is_idempotent_in_its_report(self, store):
        reminder_id = store.add_reminder("x", due_in_seconds=600)

        # Reported once, then reported as absent. This double-true was a bug.
        assert store.cancel_reminder(reminder_id) is True
        assert store.cancel_reminder(reminder_id) is False
        assert store.cancel_reminder("nonexistent") is False

    def test_cancelled_loop_never_comes_due(self, store):
        reminder_id = store.add_reminder("[loop] x", due_in_seconds=-1, repeat_interval=1)
        store.cancel_reminder(reminder_id)
        assert store.due_reminders() == []


class TestScheduler:
    def test_dispatches_and_reschedules(self, store):
        fired = []
        scheduler = Scheduler(store, dispatch=lambda r: fired.append(r["context"]), poll_seconds=1)

        store.add_reminder("[loop] poll", due_in_seconds=-1, repeat_interval=1, context="LOOP:poll")
        store.add_reminder("once", due_in_seconds=-1, context="once")

        scheduler.tick()
        assert sorted(fired) == ["LOOP:poll", "once"]

        # The one-off is done for good; the loop is pushed into the future and
        # stays live.
        rows = {r["context"]: r for r in store.list_reminders()}
        assert rows["once"]["done"] == 1
        assert rows["LOOP:poll"]["done"] == 0
        assert rows["LOOP:poll"]["due_at"] > time.time()

    def test_cancel_stops_further_fires(self, store):
        fired = []
        scheduler = Scheduler(store, dispatch=lambda r: fired.append(r["id"]), poll_seconds=1)
        reminder_id = store.add_reminder("x", due_in_seconds=-1, repeat_interval=1)

        scheduler.tick()
        store.cancel_reminder(reminder_id)
        count = len(fired)
        scheduler.tick()

        assert len(fired) == count

    def test_dispatch_failure_does_not_kill_the_tick(self, store):
        seen = []

        def explode(reminder):
            seen.append(reminder["id"])
            raise RuntimeError("boom")

        scheduler = Scheduler(store, dispatch=explode, poll_seconds=1)
        store.add_reminder("a", due_in_seconds=-1)

        scheduler.tick()  # must not raise
        assert seen

    def test_stop_ends_the_thread(self, store):
        scheduler = Scheduler(store, poll_seconds=1)
        scheduler.start()
        assert scheduler.is_alive()
        scheduler.stop()
        scheduler.join(timeout=3)
        assert not scheduler.is_alive()


class TestTaskManager:
    def test_runs_goal_and_reports(self, store):
        manager = TaskManager(store, runner=lambda goal, report: f"done:{goal}")
        task_id = manager.start("count the stars")

        _wait_for(store, task_id)

        row = store.get_task(task_id)
        assert row["status"] == "done"
        assert row["result"] == "done:count the stars"

    def test_failure_is_recorded(self, store):
        def boom(goal, report):
            raise RuntimeError("nope")

        manager = TaskManager(store, runner=boom)
        task_id = manager.start("fails")

        _wait_for(store, task_id)
        assert store.get_task(task_id)["status"] == "failed"

    def test_threads_are_pruned_when_done(self, store):
        manager = TaskManager(store, runner=lambda g, r: "ok")
        task_id = manager.start("short")

        _wait_for(store, task_id)
        time.sleep(0.05)

        # An unpruned dict grows one entry per task forever in a long server.
        assert manager.active_threads() == {}

    def test_progress_callback_updates_the_row(self, store):
        def runner(goal, report):
            report("step one")
            return "ok"

        manager = TaskManager(store, runner=runner)
        task_id = manager.start("progress")

        _wait_for(store, task_id)
        assert "finished" in store.get_task(task_id)["progress"]

    def test_on_done_callback_fires(self, store):
        seen = []
        manager = TaskManager(store, runner=lambda g, r: "ok")
        task_id = manager.start("cb", on_done=lambda row, status: seen.append(status))

        for _ in range(80):
            if seen:
                break
            time.sleep(0.02)

        assert seen == ["done"]

    def test_loop_is_clamped_to_a_floor(self, store, manager):
        # Without the floor a 1s goal would spin the scheduler.
        manager.start_loop("spin", every_seconds=1)
        reminder = store.list_reminders()[0]
        assert reminder["repeat_interval"] == 10

    def test_manager_without_runner_refuses(self, store):
        manager = TaskManager(store, runner=None)
        with pytest.raises(RuntimeError):
            manager.start("x")


class TestResultIsStoredWhole:
    """A worker's answer is the deliverable, so it is not trimmed on the way in.

    A real repo breakdown came back at 4,127 characters and was silently halved
    by a 2,000-char cap, leaving the stored result different from what ran.
    """

    def test_a_long_result_is_not_truncated(self, store):
        long_result = "x" * 4127
        manager = TaskManager(store, runner=lambda goal, report: long_result)
        task_id = manager.start("long answer")

        _wait_for(store, task_id)
        assert store.get_task(task_id)["result"] == long_result

    def test_task_status_clips_only_what_the_model_reads(self, store):
        from sage.tools import registry

        manager = TaskManager(store, runner=lambda goal, report: "y" * 4000)
        task_id = manager.start("big answer")
        _wait_for(store, task_id)

        actions = registry.build_actions(manager=manager, store=store, runtime=False)
        view = actions["task_status"](task_id=task_id)["tasks"][0]

        assert view["result_chars"] == 4000
        assert len(view["result"]) == registry.TASK_RESULT_PREVIEW
        # Clipped for the model, intact on disk.
        assert len(store.get_task(task_id)["result"]) == 4000

    def test_a_short_result_is_untouched_by_the_preview(self, store):
        from sage.tools import registry

        manager = TaskManager(store, runner=lambda goal, report: "short and whole")
        task_id = manager.start("small")
        _wait_for(store, task_id)

        actions = registry.build_actions(manager=manager, store=store, runtime=False)
        view = actions["task_status"](task_id=task_id)["tasks"][0]

        assert view["result"] == "short and whole"
        assert "result_chars" not in view


class TestWorkerInheritsTheDispatchSurface:
    """A worker runs under the policy of the surface that started it.

    The runner used to hardcode surface="cli", so a task started from a
    restricted web surface gained the local tool access that surface withheld.
    """

    def test_context_reaches_the_runner(self, store):
        seen = {}

        def runner(goal, report):
            seen.update(current_task_context())
            return "ok"

        manager = TaskManager(store, runner=runner)
        task_id = manager.start("restricted work", surface="web", local_tools=False)
        _wait_for(store, task_id)

        assert seen["surface"] == "web"
        assert seen["local_tools"] is False

    def test_context_is_cleared_after_the_task_ends(self, store):
        def runner(goal, report):
            assert current_task_context()["surface"] == "web"
            raise RuntimeError("boom")

        manager = TaskManager(store, runner=runner)
        task_id = manager.start("fails", surface="web")
        _wait_for(store, task_id)

        assert current_task_context() == {}

    def test_a_sibling_task_does_not_inherit_context(self, store):
        seen = []

        def runner(goal, report):
            seen.append(current_task_context().get("surface"))
            return "ok"

        manager = TaskManager(store, runner=runner)
        first = manager.start("one", surface="web")
        second = manager.start("two", surface="cli")
        _wait_for(store, first)
        _wait_for(store, second)

        assert sorted(seen) == ["cli", "web"]

    def test_the_default_runner_does_not_hardcode_cli(self, store, monkeypatch):
        import sage.core.orchestrator as orchestrator_module
        from sage.tasks import _default_runner

        captured = {}

        class FakeOrchestrator:
            def __init__(self, surface="cli", local_tools=True, store=None,
                         conversation_id=None):
                captured["surface"] = surface
                captured["local_tools"] = local_tools

            def run(self, goal, surface="cli"):
                captured["run_surface"] = surface
                return type("FakeJob", (), {"result": "ok"})()

        monkeypatch.setattr(orchestrator_module, "Orchestrator", FakeOrchestrator)

        manager = TaskManager(store, runner=_default_runner)
        task_id = manager.start("web work", surface="web", local_tools=False)
        _wait_for(store, task_id)

        assert captured["surface"] == "web"
        assert captured["local_tools"] is False
        assert captured["run_surface"] == "web"


class TestSharedDatabasePath:
    def test_the_store_honours_sage_db_path(self, tmp_path, monkeypatch):
        # Database resolves this too, so a test or a second instance must not
        # split conversation history and task rows across two files.
        target = tmp_path / "shared.db"
        monkeypatch.setenv("SAGE_DB_PATH", str(target))

        store = TaskStore()

        assert store.path == str(target)
        assert target.exists()

    def test_an_explicit_path_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAGE_DB_PATH", str(tmp_path / "env.db"))
        explicit = tmp_path / "explicit.db"

        assert TaskStore(str(explicit)).path == str(explicit)

    def test_get_runtime_defaults_to_the_env_path(self, tmp_path, monkeypatch):
        import sage.tasks as tasks_module

        target = tmp_path / "runtime.db"
        monkeypatch.setenv("SAGE_DB_PATH", str(target))
        # get_runtime is a process-wide singleton that other tests populate, so
        # it has to be cleared for this assertion to be about the env path.
        monkeypatch.setattr(tasks_module, "_runtime", None)

        store, _, _ = get_runtime(start_scheduler=False)

        assert store.path == str(target)


def _wait_for(store, task_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (store.get_task(task_id) or {}).get("status") in ("done", "failed"):
            return
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not finish in {timeout}s")


class TestWorkerDepth:
    """A task must not be able to spawn more tasks without bound."""

    def test_worker_flag_is_set_while_running(self, store):
        from sage.tasks import in_task_worker

        seen = {}

        def runner(goal, report):
            seen["inside"] = in_task_worker()
            return "ok"

        manager = TaskManager(store, runner=runner)
        task_id = manager.start("outer")

        _wait_for(store, task_id)
        assert seen["inside"] is True

    def test_caller_is_not_marked_as_a_worker(self, store):
        from sage.tasks import in_task_worker

        assert in_task_worker() is False

    def test_flag_is_cleared_afterwards(self, store):
        from sage.tasks import in_task_worker

        def runner(goal, report):
            raise RuntimeError("boom")

        manager = TaskManager(store, runner=runner)
        task_id = manager.start("fails")

        _wait_for(store, task_id)
        assert in_task_worker() is False

    def test_create_task_refuses_inside_a_worker(self, tmp_path):
        from sage.tasks import get_runtime
        from sage.tools import registry

        get_runtime(str(tmp_path / "d.db"), start_scheduler=False)
        registry.set_local_tools_allowed(True)

        actions = registry.build_actions()
        outer = actions["create_task"]

        # Simulate being on a worker thread.
        import sage.tasks as tasks

        tasks._worker.depth = 1
        try:
            result = outer("spawn another")
        finally:
            tasks._worker.depth = 0

        assert "error" in result
        assert "inside a task" in result["error"]


class TestReminderDelivery:
    """A fired reminder has to reach the user, not just the server log."""

    def test_notify_receives_one_off_reminders(self, store):
        seen = []
        scheduler = Scheduler(
            store, dispatch=lambda r: None, poll_seconds=1, notify=seen.append
        )
        store.add_reminder("stretch", due_in_seconds=-1, context="one-off")

        scheduler.tick()

        assert [r["title"] for r in seen] == ["stretch"]

    def test_loops_do_not_notify(self, store):
        # A recurring loop firing every 10s would otherwise spam the console.
        seen = []
        scheduler = Scheduler(
            store, dispatch=lambda r: None, poll_seconds=1, notify=seen.append
        )
        store.add_reminder("poll", due_in_seconds=-1, repeat_interval=60, context="LOOP:poll")

        scheduler.tick()

        assert seen == []

    def test_notify_failure_does_not_stop_the_tick(self, store):
        def explode(reminder):
            raise RuntimeError("db gone")

        scheduler = Scheduler(store, dispatch=lambda r: None, poll_seconds=1, notify=explode)
        store.add_reminder("stretch", due_in_seconds=-1, context="one-off")

        scheduler.tick()  # must not raise

        # Still consumed, so a broken notifier cannot cause a repeat storm.
        assert store.due_reminders() == []

    def test_notification_reaches_the_queue(self, tmp_path):
        # get_runtime is a process-wide singleton, so this drives the same
        # notify callback the server passes to a Scheduler directly.
        from sage.core.database import Database

        db = Database(str(tmp_path / "n.db"))
        db.initialize()

        store = TaskStore(str(tmp_path / "n.db"))
        scheduler = Scheduler(
            store,
            dispatch=lambda r: None,
            poll_seconds=1,
            notify=lambda r: db.add_notification("reminder", r["title"]),
        )
        store.add_reminder("drink water", due_in_seconds=-1, context="one-off")
        scheduler.tick()

        pending = db.unread_notifications()
        assert [n["body"] for n in pending] == ["drink water"]
        assert db.mark_notifications_read() == 1
        assert db.unread_notifications() == []
