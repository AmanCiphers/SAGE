import os
import sqlite3
import threading


class Database:
    def __init__(self, path=None):
        # One connection is shared across request threads, so every statement
        # has to be serialised: streaming handlers can overlap for 40s+.
        # SAGE_DB_PATH lets a test or a second instance point somewhere else;
        # the web surface keeps one conversation per database, so sharing a file
        # also shares history.
        self.path = path or os.environ.get("SAGE_DB_PATH") or "sage.db"
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()

    def initialize(self):
        with self.lock:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(id)
                )
            """)

            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS hermes_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(id)
                )
            """)

            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS route_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER,
                    handler TEXT,
                    action TEXT,
                    capability TEXT,
                    success INTEGER,
                    latency_ms INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            self.connection.commit()

    def add_notification(self, kind, body):
        with self.lock:
            self.connection.execute(
                "INSERT INTO notifications (kind, body) VALUES (?, ?)", (kind, body)
            )

            self.connection.commit()

    def unread_notifications(self, limit=20):
        with self.lock:
            rows = self.connection.execute(
                """
                    SELECT id, kind, body, created_at FROM notifications
                    WHERE read = 0 ORDER BY id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()

            return [
                dict(zip(("id", "kind", "body", "created_at"), row)) for row in rows
            ]

    def mark_notifications_read(self):
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE notifications SET read = 1 WHERE read = 0"
            )

            self.connection.commit()

            return cursor.rowcount

    def bind_hermes_session(self, conversation_id, session_id):
        """Pin a HERMES session to a conversation, once."""
        with self.lock:
            self.connection.execute(
                """
                    INSERT OR IGNORE INTO hermes_sessions
                        (conversation_id, session_id)
                    VALUES (?, ?)
                """,
                (conversation_id, session_id),
            )

            self.connection.commit()

    def get_hermes_session(self, conversation_id):
        with self.lock:
            row = self.connection.execute(
                """
                    SELECT session_id FROM hermes_sessions
                    WHERE conversation_id = ?
                    ORDER BY id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()

            return row[0] if row else None

    def record_route_outcome(
        self, conversation_id, handler, action, capability, success, latency_ms
    ):
        with self.lock:
            self.connection.execute(
                """
                    INSERT INTO route_outcomes
                        (conversation_id, handler, action, capability,
                         success, latency_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    handler,
                    action,
                    capability,
                    1 if success else 0,
                    int(latency_ms),
                ),
            )

            self.connection.commit()

    def recent_route_outcomes(self, conversation_id, limit=20):
        with self.lock:
            rows = self.connection.execute(
                """
                    SELECT handler, action, capability, success
                    FROM route_outcomes
                    WHERE conversation_id = ?
                    ORDER BY id DESC LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()

            return [dict(zip(("handler", "action", "capability", "success"), row)) for row in rows]

    def create_conversation(self):
        with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO conversations DEFAULT VALUES"
            )

            self.connection.commit()

            return cursor.lastrowid

    def add_message(self, conversation_id, role, content):
        with self.lock:
            self.connection.execute(
                """
                    INSERT INTO messages (conversation_id, role, content)
                    VALUES (?, ?, ?)
                    """,
                (conversation_id, role, content)
            )

            self.connection.commit()

    def user_messages(self, conversation_id, limit=20):
        """User turns only, newest last, numbered from 1.

        Questions like "what was my second message" need ordinals the model
        cannot reliably count by eye, and assistant turns in the way make the
        counting worse.
        """
        with self.lock:
            cursor = self.connection.execute(
                """
                    SELECT content FROM messages
                    WHERE conversation_id = ? AND role = 'user'
                    ORDER BY id ASC
                """,
                (conversation_id,),
            )
            rows = cursor.fetchall()

        turns = [{"n": i, "message": row[0]} for i, row in enumerate(rows[-limit:], 1)]

        # Number relative to the whole conversation, not the window.
        offset = len(rows) - len(turns)

        for turn in turns:
            turn["n"] += offset

        return turns

    def get_messages(self, conversation_id):
        with self.lock:
            cursor = self.connection.execute(
                """
                    SELECT role, content
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id ASC
                    """,
                (conversation_id,)
            )

            rows = cursor.fetchall()

        return [
            {
                "role": role,
                "content": content
            }
            for role, content in rows
        ]

    def get_or_create_primary_conversation(self):
        with self.lock:
            cursor = self.connection.execute(
                """
                SELECT id
                FROM conversations
                ORDER BY id ASC
                LIMIT 1
                """
            )

            row = cursor.fetchone()

        if row:
            return row[0]

        return self.create_conversation()

