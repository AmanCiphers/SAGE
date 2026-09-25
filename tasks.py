import os
import shutil
import sqlite3
import threading
import time
import uuid

DB_PATH = "sage.db"

if not os.path.exists(DB_PATH) and os.path.exists("zeus.db"):
    shutil.copyfile("zeus.db", DB_PATH)


class TaskStore:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at INT,
                updated_at INT, progress TEXT, result TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders(
                id TEXT PRIMARY KEY, title TEXT, due_at INT,
                repeat_interval INT, done INT DEFAULT 0, context TEXT
            );
            """
        )
        self.conn.commit()

    def _exec(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def create_task(self, title):
        tid = uuid.uuid4().hex[:8]
        now = int(time.time())
        self._exec(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            (tid, title, "queued", now, now, "waiting for a worker", None),
        )
        return tid

    def update_task(self, tid, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("status", "progress", "result")}
        sets = ", ".join(f"{k}=?" for k in allowed)
        self._exec(
            f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
            (*allowed.values(), int(time.time()), tid),
        )

    def get_task(self, tid):
        row = self._exec("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self):
        rows = self._exec("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def add_reminder(self, title, due_in_seconds, repeat_interval=0, context=None):
        rid = uuid.uuid4().hex[:8]
        self._exec(
            "INSERT INTO reminders VALUES (?,?,?,?,?,?)",
            (rid, title, int(time.time()) + int(due_in_seconds), int(repeat_interval), 0, context),
        )
        return rid

    def due_reminders(self):
        rows = self._exec(
            "SELECT * FROM reminders WHERE done=0 AND due_at<=?", (int(time.time()),)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_reminders(self):
        rows = self._exec("SELECT * FROM reminders ORDER BY due_at").fetchall()
        return [dict(r) for r in rows]

    def mark_done(self, rid):
        self._exec("UPDATE reminders SET done=1 WHERE id=?", (rid,))

    def requeue(self, rid, repeat_interval):
        self._exec(
            "UPDATE reminders SET done=0, due_at=? WHERE id=?",
            (int(time.time()) + repeat_interval, rid),
        )


class Scheduler(threading.Thread):
    POLL_SECONDS = 5

    def __init__(self, store, on_fire=None):
        super().__init__(daemon=True)
        self.store = store
        self.on_fire = on_fire or (lambda r: print(f"\n[reminder] {r['title']}"))

    def run(self):
        while True:
            time.sleep(self.POLL_SECONDS)
            for r in self.store.due_reminders():
                self.on_fire(r)
                if r["repeat_interval"] and r["repeat_interval"] > 0:
                    self.store.requeue(r["id"], r["repeat_interval"])
                else:
                    self.store.mark_done(r["id"])


class TaskManager:
    def __init__(self, store):
        self.store = store
        self.threads = {}
        self.callbacks = {}

    def start(self, goal, task_id=None, on_done=None):
        task_id = task_id or self.store.create_task(goal)
        self.callbacks[task_id] = on_done
        thread = threading.Thread(target=self._run, args=(goal, task_id), daemon=True)
        self.threads[task_id] = thread
        thread.start()
        return task_id

    def start_loop(self, goal, every_seconds=300):
        rid = self.store.add_reminder(
            f"[loop] {goal}",
            due_in_seconds=int(every_seconds),
            repeat_interval=int(every_seconds),
            context=f"LOOP:{goal}",
        )
        return rid

    def _run(self, goal, task_id):
        from agent import run_agent

        self.store.update_task(task_id, status="running", progress="worker started")

        def report(text):
            self.store.update_task(task_id, progress=text)

        try:
            run_agent(goal, out=report)
            self.store.update_task(task_id, status="done", progress="task finished")
            status = "done"
        except Exception as e:
            self.store.update_task(task_id, status="failed", result=str(e))
            status = "failed"
        cb = self.callbacks.get(task_id)
        if cb:
            cb(self.store.get_task(task_id), status)