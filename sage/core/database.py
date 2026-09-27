import sqlite3
import threading


class Database:
    def __init__(self, path="sage.db"):
        # One connection is shared across request threads, so every statement
        # has to be serialised: streaming handlers can overlap for 40s+.
        self.connection = sqlite3.connect(path, check_same_thread=False)
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

            self.connection.commit()

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

