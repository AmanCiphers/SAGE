"""Web approval round trip, with the model stubbed so it cannot flake."""
import json

import pytest
from fastapi.testclient import TestClient

from sage.core.job import JobStatus


class StubHandler:
    """Yields one approval frame, then records whether a replay came through."""

    def __init__(self, approved_command=None):
        self.approved_command = approved_command
        self.replayed = []

    def run(self, message, conversation=None, model=None, surface="cli",
            approved_command=None, conversation_id=None):
        self.replayed.append(approved_command)

        if approved_command is None:
            yield {"type": "tool_pending", "name": "bash", "arguments": {}}
            yield {
                "type": "tool", "name": "bash", "arguments": {},
                "result": {"error": "approval required", "approval_required": True},
            }
            yield {
                "type": "approval_required", "name": "bash",
                "command": "rm -rf /tmp/sage-web-victim",
                "reason": "recursive delete",
            }
        else:
            # A real replay runs the command, so it reports a tool event. Leaving
            # this out would be caught by the verifier's no-fabricated-result
            # check, which is the point of that check.
            yield {
                "type": "tool", "name": "bash", "arguments": {},
                "result": {"stdout": "removed", "exit_code": 0},
            }
            yield {"type": "text", "delta": "removed it"}
            yield {"type": "done", "content": "removed it"}


class AnalyzerStub:
    def analyze(self, message):
        from sage.core.analysis import TaskAnalysis

        return TaskAnalysis(
            intent="i", action="act", delegate=False, target="local", tools=[]
        )


@pytest.fixture
def client_and_handler(monkeypatch):
    from sage.web import api

    handler = StubHandler()
    monkeypatch.setattr(api.orchestrator, "handler", handler)
    monkeypatch.setattr(api.orchestrator, "local_tools", True)
    monkeypatch.setattr(api.orchestrator, "analyzer", AnalyzerStub())
    return TestClient(api.app), handler


def _frames(text):
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ")]


def test_stream_carries_exactly_one_approval_frame(client_and_handler):
    client, _ = client_and_handler

    with client.stream("POST", "/chat/stream",
                       json={"message": "delete /tmp/sage-web-victim"}) as res:
        frames = _frames(res.read().decode())

    approvals = [f["approval"] for f in frames if f.get("approval")]

    assert len(approvals) == 1
    assert approvals[0]["target"] == "rm -rf /tmp/sage-web-victim"
    assert "recursive delete" in approvals[0]["description"]


def test_declining_runs_nothing(client_and_handler):
    client, handler = client_and_handler

    with client.stream("POST", "/chat/stream",
                       json={"message": "delete /tmp/sage-web-victim"}) as res:
        approval = [f["approval"] for f in _frames(res.read().decode())
                    if f.get("approval")][0]

    res = client.post("/chat/approve", json={"id": approval["id"], "approved": False})

    assert res.json()["status"] == "declined"
    # Only the original turn: nothing was replayed.
    assert handler.replayed == [None]


def test_approving_replays_with_that_command(client_and_handler):
    client, handler = client_and_handler

    with client.stream("POST", "/chat/stream",
                       json={"message": "delete /tmp/sage-web-victim"}) as res:
        approval = [f["approval"] for f in _frames(res.read().decode())
                    if f.get("approval")][0]

    res = client.post("/chat/approve", json={"id": approval["id"], "approved": True})

    assert res.json()["status"] == JobStatus.COMPLETED.value
    assert handler.replayed[-1] == "rm -rf /tmp/sage-web-victim"


def test_an_approval_cannot_be_replayed_twice(client_and_handler):
    client, handler = client_and_handler

    with client.stream("POST", "/chat/stream",
                       json={"message": "delete /tmp/sage-web-victim"}) as res:
        approval = [f["approval"] for f in _frames(res.read().decode())
                    if f.get("approval")][0]

    first = client.post("/chat/approve", json={"id": approval["id"], "approved": True})
    second = client.post("/chat/approve", json={"id": approval["id"], "approved": True})

    assert first.json()["status"] == JobStatus.COMPLETED.value
    assert second.json()["status"] == "failed"
    assert len(handler.replayed) == 2
