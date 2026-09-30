"""Web approval round trip, with the model stubbed so it cannot flake."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sage.core.job import JobStatus


def test_every_console_route_is_rewritten_to_the_backend():
    """The console talks to paths the backend serves, and Next has to pass them on.

    /chat/approve and /notifications were missing, so an approval reply and a
    completion notification both landed on Next and 404'd instead of reaching
    the API. A dropped rewrite is invisible until the exact flow is used, which
    is the worst time to find it.
    """
    config = Path(__file__).resolve().parents[1] / "web" / "next.config.mjs"
    text = config.read_text()

    for route in ("/chat", "/chat/stream", "/chat/approve", "/notifications",
                  "/info", "/api/health"):
        assert f'source: "{route}"' in text, f"{route} is not rewritten"


class StubHandler:
    """Yields one approval frame, then records whether a replay came through."""

    def __init__(self, approved_command=None):
        self.approved_command = approved_command
        self.replayed = []

    def run(self, message, conversation=None, model=None, surface="cli",
            approved_command=None, conversation_id=None, local_tools=True):
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


def api_status(value):
    """A real JobStatus, so _save_assistant's comparison is exercised."""
    from sage.core.job import JobStatus

    return JobStatus(value)


class TestSpokenLine:
    """The console shows the real answer and a spoken companion.

    The spoken line is what a voice mode would read, so it ships in the same
    payload and must never be a claim about work that did not happen.
    """

    def _app(self, monkeypatch, result, status, spoken="all done"):
        import sage.web.api as api

        job = SimpleNamespace(
            result=result,
            status=status,
            error=None,
            approval=SimpleNamespace(as_payload=lambda: {"id": "a1", "target": "rm -rf /tmp/x"}),
        )

        monkeypatch.setattr(
            api.orchestrator, "run", lambda *a, **kw: job, raising=False
        )
        # spoken=None leaves the real _spoken in place, so a test can exercise
        # the guard that keeps a broken summarizer off the reply.
        if spoken is not None:
            monkeypatch.setattr(
                api, "_spoken", lambda answer, question: spoken, raising=False
            )
        monkeypatch.setattr(api.db, "add_message", lambda *a, **kw: None)
        monkeypatch.setattr(api.db, "get_messages", lambda *a, **kw: [])

        return TestClient(api.app)

    def test_completed_turn_returns_a_summary(self, monkeypatch):
        client = self._app(monkeypatch, "The build passed.", api_status("completed"))

        body = client.post("/chat", json={"message": "did it pass?"}).json()

        assert body["response"] == "The build passed."
        assert body["summary"] == "all done"

    @pytest.mark.parametrize("status", ["failed", "needs_approval"])
    def test_unfinished_turn_gets_no_summary(self, monkeypatch, status):
        # A confident "all done" over a parked approval or a failure is a claim
        # about work that never ran, so the field is empty rather than absent.
        client = self._app(monkeypatch, "I cannot do that.", api_status(status))

        body = client.post("/chat", json={"message": "delete /tmp/x"}).json()

        assert body["summary"] == ""

    @pytest.mark.parametrize("broken", ["construct", "summarize"])
    def test_summary_never_fails_the_reply(self, monkeypatch, broken):
        # Patches the summarizer, not _spoken, so the real guard is under test.
        import sage.web.api as api

        client = self._app(
            monkeypatch, "done", api_status("completed"), spoken=None
        )

        class Boom:
            def __init__(self, *a, **kw):
                if broken == "construct":
                    raise RuntimeError("no api key")

            def summarize(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(api, "SpokenSummarizer", Boom)

        body = client.post("/chat", json={"message": "go"}).json()

        assert body["status"] == "completed"
        assert body["response"] == "done"
        assert body["summary"] == ""


class TestSpokenStreamFrame:
    """Over SSE the spoken line is its own frame, after done.

    Keeping it out of the done frame is what lets the full answer seal on
    screen immediately: the done frame arrives without waiting on a second
    model call, and the spoken line lands just behind it.
    """

    def _stream(self, monkeypatch, status, spoken):
        import sage.web.api as api

        def run_stream(*a, **kw):
            yield {"delta": "Your IP is 45.115.179.166."}
            yield {
                "done": True,
                "response": "Your IP is 45.115.179.166.",
                "status": status,
                "job": SimpleNamespace(result="x", status=JobStatus(status), error=None),
            }

        monkeypatch.setattr(api.orchestrator, "run_stream", run_stream, raising=False)
        monkeypatch.setattr(api, "_spoken", lambda a, q: spoken, raising=False)
        monkeypatch.setattr(api.db, "add_message", lambda *a, **kw: None)
        monkeypatch.setattr(api.db, "get_messages", lambda *a, **kw: [])

        res = TestClient(api.app).post("/chat/stream", json={"message": "my ip?"})

        return [
            json.loads(line[5:])
            for line in res.text.splitlines()
            if line.startswith("data: ")
        ]

    def test_spoken_line_follows_done(self, monkeypatch):
        frames = self._stream(monkeypatch, "completed", "Your IP is on the way.")

        done_at = next(i for i, f in enumerate(frames) if f.get("done"))
        say_at = next(i for i, f in enumerate(frames) if f.get("say"))

        assert say_at == done_at + 1
        assert frames[say_at]["say"] == "Your IP is on the way."

    def test_no_spoken_frame_for_an_unfinished_turn(self, monkeypatch):
        frames = self._stream(monkeypatch, "failed", "everything is fine")

        assert not any(f.get("say") for f in frames)

    def test_no_spoken_frame_when_generation_returns_nothing(self, monkeypatch):
        # An absent line is normal, not an error, and must not close the
        # stream early or invent a frame.
        frames = self._stream(monkeypatch, "completed", None)

        assert any(f.get("done") for f in frames)
        assert not any(f.get("say") for f in frames)
