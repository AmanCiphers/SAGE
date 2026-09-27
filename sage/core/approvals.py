"""Parked HERMES turns waiting on the user's yes/no.

A HERMES approval cannot be answered inside the subprocess: the pending-request
queue lives in the child's memory and is gone once it exits. So the turn is
parked here, the client is asked, and approval replays the same task with
process-scoped ``--yolo``.

Entries are in-process and deliberately expire. An unanswered approval should
not stay replayable forever.
"""

import threading
import time
import uuid

TTL_SECONDS = 900


class PendingApproval:
    def __init__(self, task, model=None, session_id=None, conversation_id=None,
                 target="", description="", approved_command=None):
        self.id = uuid.uuid4().hex
        self.task = task
        self.model = model
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.target = target
        self.description = description
        # Set when SAGE's own bash tool refused the command. The approved replay
        # runs this one command instead of lifting the gate for the whole turn.
        self.approved_command = approved_command
        self.created_at = time.time()

    @property
    def expired(self):
        return time.time() - self.created_at > TTL_SECONDS

    def as_payload(self):
        return {
            "id": self.id,
            "target": self.target,
            "description": self.description,
            "task": self.task,
        }

    def as_event(self):
        # Nested on purpose: a flat "approval": true alongside the fields made
        # every client reassemble the payload by hand.
        return {"approval": self.as_payload()}


class ApprovalRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def add(self, pending):
        with self._lock:
            self._prune()
            self._items[pending.id] = pending

        return pending

    def get(self, approval_id):
        with self._lock:
            self._prune()
            return self._items.get(approval_id)

    def resolve(self, approval_id):
        """Fetch and forget, so one approval cannot be replayed twice."""
        with self._lock:
            self._prune()
            return self._items.pop(approval_id, None)

    def _prune(self):
        for key in [k for k, v in self._items.items() if v.expired]:
            del self._items[key]


registry = ApprovalRegistry()
