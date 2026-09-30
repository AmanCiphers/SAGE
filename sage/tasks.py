"""Background tasks, reminders, and repeating loops.

Ports the harness task system with three corrections: loops now run their goal
instead of only scheduling it, loops and reminders can be cancelled, and the
finished-thread bookkeeping is pruned so a long-lived server does not grow a
dict entry per task forever.
"""

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_POLL_SECONDS = 5

# A worker result is the deliverable, so it is stored whole. Bounding what the
# *model* is shown when it polls a task is a separate concern and happens at
# the tool boundary, not by destroying the stored result.
ERROR_MAX_CHARS = 2000

# The columns a caller may write on a task. Explicit rather than reflecting
# over the schema, so a value can never land in a column that means something
# else and update_task stays a narrow, auditable surface.
_UPDATABLE = frozenset({
    "status", "progress", "result", "stage", "error", "summary",
    "started_at", "finished_at", "cancel_requested",
})


# A task's runner is a full Orchestrator, whose model can call create_task
# again. That is unbounded thread growth driven by the model, so a worker
# refuses to spawn more workers. Thread-local, because each task runs on its
# own thread and a sibling task must not inherit the restriction.
_worker = threading.local()

# The execution policy the worker was dispatched with. Thread-local for the
# same reason as the depth flag, and passed this way rather than through the
# runner signature so an injected two-argument test runner keeps working.
_context = threading.local()


def in_task_worker():
    """True when the current thread is running a spawned task's goal."""
    return getattr(_worker, "depth", 0) > 0


def current_task_context():
    """The dispatch context for the task running on this thread, if any."""
    return getattr(_context, "value", None) or {}


class TaskStore:
    def __init__(self, path=None):
        # Mirrors Database: SAGE_DB_PATH points the task tables at the same
        # file the conversation history uses, so a test or a second instance
        # does not silently split into two databases.
        self.path = str(path or os.environ.get("SAGE_DB_PATH") or "sage.db")
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
                CREATE TABLE IF NOT EXISTS task_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    seq INTEGER,
                    kind TEXT,
                    stage TEXT,
                    name TEXT,
                    detail TEXT,
                    ok INTEGER,
                    created_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS task_events_task
                    ON task_events(task_id, seq);
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
            self._migrate_tasks()
            self.connection.commit()

    # Columns added after the first release. A live database already has a
    # tasks table, so CREATE TABLE IF NOT EXISTS leaves it exactly as it was and
    # every write to a new column would fail. Adding them here means an
    # existing install migrates in place and keeps its rows.
    _TASK_COLUMNS = (
        ("stage", "TEXT"),
        ("error", "TEXT"),
        ("summary", "TEXT"),
        ("conversation_id", "TEXT"),
        ("surface", "TEXT"),
        ("local_tools", "INTEGER"),
        ("cancel_requested", "INTEGER DEFAULT 0"),
        ("started_at", "INTEGER"),
        ("finished_at", "INTEGER"),
    )

    def _migrate_tasks(self):
        existing = {row["name"] for row in
                    self.connection.execute("PRAGMA table_info(tasks)")}

        for name, kind in self._TASK_COLUMNS:
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE tasks ADD COLUMN {name} {kind}"
                )

    def event_columns(self):
        """Column names on the tasks table, for migration checks and tests."""
        with self.lock:
            return {row["name"] for row in
                    self.connection.execute("PRAGMA table_info(tasks)")}

    def _exec(self, sql, params=()):
        with self.lock:
            cursor = self.connection.execute(sql, params)
            self.connection.commit()

            return cursor

    def create_task(self, title, conversation_id=None, surface="cli",
                    local_tools=True):
        task_id = uuid.uuid4().hex[:8]
        now = int(time.time())
        # Named columns, not a positional VALUES list: the table gains columns
        # over time, and a positional insert breaks the moment it does.
        self._exec(
            """
            INSERT INTO tasks(
                id, title, status, created_at, updated_at, progress, result,
                conversation_id, surface, local_tools, cancel_requested
            ) VALUES (?,?,?,?,?,?,?,?,?,?,0)
            """,
            (task_id, title, "queued", now, now, "waiting for a worker", None,
             conversation_id, surface, 1 if local_tools else 0),
        )

        return task_id

    def update_task(self, task_id, **fields):
        allowed = {k: v for k, v in fields.items() if k in _UPDATABLE}

        if not allowed:
            return

        sets = ", ".join(f"{k}=?" for k in allowed)
        self._exec(
            f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
            (*allowed.values(), int(time.time()), task_id),
        )

    def add_event(self, task_id, kind, stage=None, name=None, detail=None,
                  ok=None):
        """Append one entry to a task's timeline.

        The sequence number is the next count for the task rather than a global
        autoincrement, so the client can order a single task's events and tell
        where new ones belong.
        """
        row = self._exec(
            "SELECT COALESCE(MAX(seq), 0) AS top FROM task_events WHERE task_id=?",
            (task_id,),
        ).fetchone()

        self._exec(
            """
            INSERT INTO task_events(task_id, seq, kind, stage, name, detail, ok, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (task_id, (row["top"] or 0) + 1, kind, stage, name, detail,
             None if ok is None else int(bool(ok)), int(time.time())),
        )

    def events(self, task_id, after_seq=0, limit=500):
        rows = self._exec(
            """
            SELECT * FROM task_events
            WHERE task_id=? AND seq>?
            ORDER BY seq LIMIT ?
            """,
            (task_id, after_seq, limit),
        ).fetchall()

        return [dict(row) for row in rows]

    def request_cancel(self, task_id):
        self._exec("UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=?",
                   (int(time.time()), task_id))

    def cancel_requested(self, task_id):
        row = self._exec(
            "SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)
        ).fetchone()

        return bool(row and row["cancel_requested"])

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


def _stage_detail(event):
    """The useful part of a stage frame, for a one-line timeline entry."""
    if event.get("handler"):
        return event["handler"]

    tools = event.get("tools")

    if isinstance(tools, (list, tuple)) and tools:
        return ", ".join(str(t) for t in tools)

    return event.get("name")


class WorkerLimit(RuntimeError):
    """Raised when every worker slot is already in use."""


# A worker is a full model loop with tool access, so an unbounded count is an
# unbounded number of concurrent model calls and shells. Three keeps a genuine
# fan-out useful while putting a ceiling on what one request can spend.
MAX_WORKERS = 3

# The pipeline reports these as job status values; the task table uses its own.
_STATUS_MAP = {"completed": "done"}


class TaskManager:
    """Runs goals on background threads and tracks their progress."""

    def __init__(self, store, runner=None, stream_runner=None, max_workers=MAX_WORKERS,
                 on_complete=None):
        self.store = store
        self.runner = runner
        # Preferred when present: it yields the pipeline's real events, so the
        # timeline is observed rather than reconstructed. ``runner`` stays for
        # the plain string contract.
        self.stream_runner = stream_runner
        self.max_workers = max_workers
        # Fires for every task that ends, however it was started, so a
        # completion reaches the user without each caller having to remember
        # to wire it.
        self.on_complete = on_complete
        self.threads = {}
        self._lock = threading.Lock()

    def at_capacity(self):
        with self._lock:
            return len([t for t in self.threads.values() if t.is_alive()]) >= self.max_workers

    def start(self, goal, task_id=None, on_done=None, surface="cli", local_tools=True,
              conversation_id=None, chat_store=None):
        """Run a goal in the background and return its task id.

        The dispatch surface and its tool policy travel with the task. A worker
        that hardcoded its own surface would run with permissions the surface
        it was launched from never granted.
        """
        if not self.runner and not self.stream_runner:
            raise RuntimeError("TaskManager has no runner configured")

        if self.at_capacity():
            raise WorkerLimit(
                f"{self.max_workers} workers are already running; "
                "wait for one to finish before starting another"
            )

        task_id = task_id or self.store.create_task(
            goal, conversation_id=conversation_id, surface=surface,
            local_tools=local_tools,
        )

        def work():
            self.store.update_task(
                task_id, status="running", progress="worker started",
                stage="starting", started_at=int(time.time()),
            )
            _worker.depth = getattr(_worker, "depth", 0) + 1
            _context.value = {
                "surface": surface,
                "local_tools": local_tools,
                "conversation_id": conversation_id,
                "chat_store": chat_store,
            }

            try:
                if self.stream_runner is not None:
                    result, status, error = self._consume(goal, task_id)
                else:
                    result = self.runner(
                        goal, lambda text: self.store.update_task(task_id, progress=text)
                    )
                    status, error = "done", None

                self._finish(task_id, result, status, error)
            except Exception as failure:
                # An error is not an answer. It belongs in its own column, so a
                # failed task cannot be mistaken for one that produced text.
                self._finish(task_id, None, "failed", f"{type(failure).__name__}: {failure}")
                status = "failed"
            finally:
                _context.value = None
                _worker.depth -= 1
                with self._lock:
                    self.threads.pop(task_id, None)

            row = self.store.get_task(task_id)

            if self.on_complete:
                # Best-effort: a broken notifier must not turn a finished task
                # into a failed one, and the work is already safely stored.
                try:
                    self.on_complete(row, status)
                except Exception as error:
                    print(f"[TASK] completion notice failed for {task_id}: {error}")

            if on_done:
                on_done(row, status)

        thread = threading.Thread(target=work, daemon=True, name=f"sage-task-{task_id}")

        with self._lock:
            self.threads[task_id] = thread

        thread.start()

        return task_id

    def _finish(self, task_id, result, status, error):
        now = int(time.time())
        fields = {"status": status, "finished_at": now, "stage": status}

        if status == "done":
            fields.update(progress="finished", result=str(result or ""))
        elif status == "cancelled":
            fields.update(progress="cancelled")
        else:
            fields.update(progress=status, error=(error or "")[:ERROR_MAX_CHARS] or None)

        self.store.update_task(task_id, **fields)

    def _consume(self, goal, task_id):
        """Drain the pipeline's event stream into the task's timeline.

        Each stage and tool event is something the worker really did, recorded
        as it happens, so a progress query can report facts instead of a
        fraction guessed from elapsed time.
        """
        final = None
        error = None
        approval = None

        for event in self.stream_runner(goal):
            if "job" in event:
                final = event
                continue

            if self.store.cancel_requested(task_id):
                return None, "cancelled", None

            if event.get("approval") is not None:
                # A worker never decides this for itself. It parks and the
                # approval is answered from the worker space, on the same terms
                # as one raised in the main chat.
                approval = event["approval"]
                self.store.add_event(
                    task_id, kind="approval", name=approval.get("id"),
                    detail=approval.get("description"),
                )
            elif "tool" in event:
                self.store.add_event(
                    task_id, kind="tool", name=event.get("tool"),
                    detail=event.get("summary"), ok=event.get("ok"),
                )
            elif event.get("stage"):
                self.store.add_event(
                    task_id, kind="stage", stage=event["stage"],
                    detail=_stage_detail(event),
                )
                self.store.update_task(task_id, stage=event["stage"])
            elif event.get("error"):
                error = event["error"]
                self.store.add_event(task_id, kind="error", detail=error)

        if approval is not None:
            return None, "needs_approval", "waiting on your approval"

        if final is None:
            return None, "failed", "worker stream ended without a result"

        status = _STATUS_MAP.get(final.get("status"), final.get("status") or "failed")

        return final.get("response") or "", status, error

    def cancel(self, task_id):
        """Ask a running task to stop, and report whether one was running."""
        row = self.store.get_task(task_id)

        if not row or row.get("status") not in ("queued", "running"):
            return False

        self.store.request_cancel(task_id)

        return True

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


def _orchestrator_for_this_task():
    """The orchestrator a worker runs, carrying its dispatch policy through.

    Shared by the plain and streaming runners so neither can drift back to
    inventing its own surface.
    """
    from sage.core.orchestrator import Orchestrator

    context = current_task_context()

    return Orchestrator(
        surface=context.get("surface") or "cli",
        local_tools=bool(context.get("local_tools", True)),
        store=context.get("chat_store"),
        conversation_id=context.get("conversation_id"),
    ), (context.get("surface") or "cli")


def _default_runner(goal, report):
    """Run a background goal through the same pipeline the user talks to.

    Imported lazily: the orchestrator imports this module, so a top-level
    import would be circular.
    """
    orchestrator, surface = _orchestrator_for_this_task()

    report(f"worker started on {surface}")

    return orchestrator.run(goal, surface=surface).result


def _default_stream_runner(goal):
    """The same goal, but yielding the pipeline's events as it works.

    This is what lets a worker's progress be observed rather than asserted.
    """
    orchestrator, surface = _orchestrator_for_this_task()

    yield from orchestrator.run_stream(goal, surface=surface)


_runtime = None
_runtime_lock = threading.Lock()


def get_runtime(db_path=None, start_scheduler=True, notify=None, on_task_complete=None):
    """Return the process-wide task store, manager, and scheduler.

    Built once on first use. Without this the task tools were advertised to the
    model but never bound, so every call to them failed as an unknown tool.
    """
    global _runtime

    with _runtime_lock:
        if _runtime is not None:
            return _runtime

        store = TaskStore(db_path)
        manager = TaskManager(
            store, runner=_default_runner, stream_runner=_default_stream_runner,
            on_complete=on_task_complete,
        )

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
