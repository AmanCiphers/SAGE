"""Background tasks, reminders, and repeating loops.

Ports the harness task system with three corrections: loops now run their goal
instead of only scheduling it, loops and reminders can be cancelled, and the
finished-thread bookkeeping is pruned so a long-lived server does not grow a
dict entry per task forever.
"""

import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_POLL_SECONDS = 5


# A task's runner is a full Orchestrator, whose model can call create_task
# again. That is unbounded thread growth driven by the model, so a worker
# refuses to spawn more workers. Thread-local, because each task runs on its
# own thread and a sibling task must not inherit the restriction.
_worker = threading.local()


def in_task_worker():
    """True when the current thread is running a spawned task's goal."""
    return getattr(_worker, "depth", 0) > 0


class TaskStore:
    def __init__(self, path="sage.db"):
        self.path = str(path)
        self.lock = threading.Lock()
        self.connection = self._connect()
        self.initialize()

    def _connect(self):
        # Allow ":memory:" and ordinary paths alike.
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row

        return connection

    def initialize(self):
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    status TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    progress TEXT,
                    result TEXT
                );
                CREATE TABLE IF NOT EXISTS reminders(
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    due_at INTEGER,
                    repeat_interval INTEGER,
                    done INTEGER DEFAULT 0,
                    context TEXT
                );
                """
            )
            self.connection.commit()

    def _exec(self, sql, params=()):
        with self.lock:
            cursor = self.connection.execute(sql, params)
            self.connection.commit()

            return cursor

    def create_task(self, title):
        task_id = uuid.uuid4().hex[:8]
        now = int(time.time())
        self._exec(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            (task_id, title, "queued", now, now, "waiting for a worker", None),
        )

        return task_id

    def update_task(self, task_id, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("status", "progress", "result")}

        if not allowed:
            return

        sets = ", ".join(f"{k}=?" for k in allowed)
        self._exec(
            f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
            (*allowed.values(), int(time.time()), task_id),
        )

    def get_task(self, task_id):
        row = self._exec("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

        return dict(row) if row else None

    def list_tasks(self, limit=25):
        # created_at has one-second resolution, so tasks created in the same
        # second tie. rowid breaks the tie in insertion order.
        rows = self._exec(
            "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()

        return [dict(row) for row in rows]

    def add_reminder(self, title, due_in_seconds, repeat_interval=0, context=None):
        reminder_id = uuid.uuid4().hex[:8]
        self._exec(
            "INSERT INTO reminders VALUES (?,?,?,?,?,?)",
            (
                reminder_id,
                title,
                int(time.time()) + int(due_in_seconds),
                int(repeat_interval),
                0,
                context,
            ),
        )

        return reminder_id

    def due_reminders(self):
        rows = self._exec(
            "SELECT * FROM reminders WHERE done=0 AND due_at<=?", (int(time.time()),)
        ).fetchall()

        return [dict(row) for row in rows]

    def list_reminders(self, limit=50):
        rows = self._exec(
            "SELECT * FROM reminders ORDER BY due_at LIMIT ?", (limit,)
        ).fetchall()

        return [dict(row) for row in rows]

    def mark_done(self, reminder_id):
        self._exec("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))

    def cancel_reminder(self, reminder_id):
        """Stop a reminder or loop and return whether it was actually pending."""
        cursor = self._exec("UPDATE reminders SET done=1 WHERE id=? AND done=0", (reminder_id,))

        return cursor.rowcount > 0

    def requeue(self, reminder_id, repeat_interval):
        self._exec(
            "UPDATE reminders SET done=0, due_at=? WHERE id=?",
            (int(time.time()) + int(repeat_interval), reminder_id),
        )


class Scheduler(threading.Thread):
    """Polls for due reminders and fires them through a dispatch callback."""

    def __init__(self, store, dispatch=None, poll_seconds=DEFAULT_POLL_SECONDS,
                 notify=None):
        super().__init__(daemon=True, name="sage-scheduler")
        self.store = store
        self.poll_seconds = poll_seconds
        self.dispatch = dispatch or (lambda reminder: print(f"[reminder] {reminder['title']}"))
        # A fired reminder has to reach the user, not just the server log.
        self.notify = notify
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as error:
                print(f"[SCHEDULER] tick failed: {error}")

    def tick(self):
        for reminder in self.store.due_reminders():
            try:
                self.dispatch(reminder)
            except Exception as error:
                print(f"[SCHEDULER] dispatch failed for {reminder['id']}: {error}")

            if self.notify is not None and not (reminder.get("context") or "").startswith("LOOP:"):
                try:
                    self.notify(reminder)
                except Exception as error:
                    print(f"[SCHEDULER] notify failed for {reminder['id']}: {error}")

            interval = reminder.get("repeat_interval") or 0

            if interval > 0:
                self.store.requeue(reminder["id"], interval)
            else:
                self.store.mark_done(reminder["id"])

    def stop(self):
        self._stop.set()


class TaskManager:
    """Runs goals on background threads and tracks their progress."""

    def __init__(self, store, runner=None):
        self.store = store
        self.runner = runner
        self.threads = {}
        self._lock = threading.Lock()

    def start(self, goal, task_id=None, on_done=None):
        task_id = task_id or self.store.create_task(goal)
        runner = self.runner

        if not runner:
            raise RuntimeError("TaskManager has no runner configured")

        def work():
            self.store.update_task(task_id, status="running", progress="worker started")
            _worker.depth = getattr(_worker, "depth", 0) + 1

            try:
                result = runner(goal, lambda text: self.store.update_task(task_id, progress=text))
                self.store.update_task(
                    task_id, status="done", progress="finished", result=str(result)[:2000]
                )
                status = "done"
            except Exception as error:
                self.store.update_task(task_id, status="failed", result=str(error)[:2000])
                status = "failed"
            finally:
                _worker.depth -= 1
                with self._lock:
                    self.threads.pop(task_id, None)

            if on_done:
                on_done(self.store.get_task(task_id), status)

        thread = threading.Thread(target=work, daemon=True, name=f"sage-task-{task_id}")

        with self._lock:
            self.threads[task_id] = thread

        thread.start()

        return task_id

    def start_loop(self, goal, every_seconds=300):
        """Repeat a goal on a fixed interval until cancelled."""
        every_seconds = max(int(every_seconds), 10)

        return self.store.add_reminder(
            f"[loop] {goal}",
            due_in_seconds=every_seconds,
            repeat_interval=every_seconds,
            context=f"LOOP:{goal}",
        )

    def active_threads(self):
        with self._lock:
            return {k: t for k, t in self.threads.items() if t.is_alive()}


def _default_runner(goal, report):
    """Run a background goal through the same pipeline the user talks to.

    Imported lazily: the orchestrator imports this module, so a top-level
    import would be circular.
    """
    from sage.core.orchestrator import Orchestrator

    report("worker started")

    return Orchestrator().run(goal, surface="cli").result


_runtime = None
_runtime_lock = threading.Lock()


def get_runtime(db_path="sage.db", start_scheduler=True, notify=None):
    """Return the process-wide task store, manager, and scheduler.

    Built once on first use. Without this the task tools were advertised to the
    model but never bound, so every call to them failed as an unknown tool.
    """
    global _runtime

    with _runtime_lock:
        if _runtime is not None:
            return _runtime

        store = TaskStore(db_path)
        manager = TaskManager(store, runner=_default_runner)

        def dispatch(reminder):
            context = reminder.get("context") or ""

            if context.startswith("LOOP:"):
                goal = context[len("LOOP:") :]
                print(f"[LOOP] running {goal!r}")
                manager.start(goal)
            else:
                print(f"[REMINDER] {reminder['title']}")

        scheduler = Scheduler(store, dispatch=dispatch, notify=notify)

        if start_scheduler:
            scheduler.start()

        _runtime = (store, manager, scheduler)

        return _runtime
