"""Orchestrator-level HERMES behaviour: session pinning and approval round-trip.

A fake HERMES stands in for the subprocess, so these cover the wiring the unit
tests cannot see: that a conversation keeps one session across turns, and that
a parked approval replays exactly once.
"""

import pytest

from sage.core.analysis import TaskAnalysis
from sage.core.approvals import ApprovalRegistry
from sage.core.database import Database
from sage.core.hermes import ApprovalRequired
from sage.core.job import JobStatus
from sage.core.job_analyzer import Analyzer
from sage.core.orchestrator import Orchestrator
from sage.core.verifier import Verifier

APPROVAL_TEXT = (
    "This action is potentially dangerous (delete in root path). "
    "Asking the user for approval.\n\n**Target:**\n```\nrm -rf /tmp/thing\n```"
)


class FakeHermes:
    """Records calls and can be told to demand approval once."""

    def __init__(self, approval_first=False):
        self.calls = []
        self.approval_first = approval_first
        # The real CLI opens a fresh session per one-shot and only keeps the id
        # stable when a turn resumes, so the fake has to rotate too.
        self.sessions = ["20260927_000000_aaaaaa"]
        self.counter = 0
        self.fail = None

    def latest_session_id(self):
        return self.sessions[-1]

    def run(self, task, model=None, session_id=None, yolo=False):
        self.calls.append(
            {"task": task, "model": model, "session_id": session_id, "yolo": yolo}
        )

        if self.fail:
            raise self.fail

        if not session_id:
            self.counter += 1
            self.sessions.append(f"20260927_00000{self.counter}_new{self.counter:04d}")

        if self.approval_first and not yolo and len(self.calls) == 1:
            raise ApprovalRequired("rm -rf /tmp/thing", "delete in root path")

        return f"answer:{task}"


# A multi-tool plan, so the routing backstop does not demote this to SAGE --
# a single narrow tool here would be handled natively by design.
def analysis(delegate=True, tools=("web_search", "fetch_url")):
    return TaskAnalysis(
        intent="i", action="research", delegate=delegate, target="hermes",
        tools=list(tools),
    )


def build(hermes, store, conversation_id=1, fix_analysis=None):
    orchestrator = Orchestrator(
        analyzer=AnalyzerStub(fix_analysis or analysis()),
        handler=object(),
        hermes=hermes,
        verifier=Verifier(),
        store=store,
        conversation_id=conversation_id,
    )
    return orchestrator


class AnalyzerStub:
    def __init__(self, result):
        self.result = result

    def analyze(self, message):
        return self.result


@pytest.fixture
def store():
    db = Database(":memory:")
    db.initialize()
    return db


class TestSessionPinning:
    def test_first_turn_pins_the_session_it_created(self, store):
        hermes = FakeHermes()
        orchestrator = build(hermes, store)

        orchestrator.run("do a thing")

        # HERMES opened a new session for the turn; that is the one to pin,
        # not the one that existed beforehand.
        assert store.get_hermes_session(1) == hermes.sessions[-1] != "20260927_000000_aaaaaa"

    def test_second_turn_resumes_the_pinned_session(self, store):
        hermes = FakeHermes()
        orchestrator = build(hermes, store)

        orchestrator.run("first")
        pinned = store.get_hermes_session(1)
        orchestrator.run("second")

        assert hermes.calls[0]["session_id"] is None
        assert hermes.calls[1]["session_id"] == pinned

    def test_pinning_is_per_conversation(self, store):
        hermes = FakeHermes()
        store.bind_hermes_session(2, "20260101_000000_bbbbbb")

        orchestrator = build(hermes, store, conversation_id=1)
        orchestrator.run("x")

        assert store.get_hermes_session(1) != "20260101_000000_bbbbbb"
        assert store.get_hermes_session(2) == "20260101_000000_bbbbbb"

    def test_unchanged_session_is_not_rebound(self, store):
        hermes = FakeHermes()
        hermes.sessions = ["20260927_000000_aaaaaa"]
        store.bind_hermes_session(1, "20200101_000000_zzzzzz")

        orchestrator = build(hermes, store)
        orchestrator.run("x")

        # latest == previous means HERMES did not create a new session.
        assert store.get_hermes_session(1) == "20200101_000000_zzzzzz"

    def test_no_store_means_stateless_but_works(self):
        hermes = FakeHermes()
        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis()),
            handler=object(),
            hermes=hermes,
            verifier=Verifier(),
        )

        job = orchestrator.run("x")

        assert job.status is JobStatus.COMPLETED
        assert hermes.calls[0]["session_id"] is None


class TestApprovalRoundTrip:
    def test_approval_parks_the_turn(self, store, monkeypatch):
        monkeypatch.setattr(
            "sage.core.orchestrator.approval_registry", ApprovalRegistry()
        )
        hermes = FakeHermes(approval_first=True)
        orchestrator = build(hermes, store)

        job = orchestrator.run("delete the thing")

        assert job.status is JobStatus.NEEDS_APPROVAL
        assert job.approval is not None
        assert job.approval.target == "rm -rf /tmp/thing"
        # The dangerous action did not run.
        assert len(hermes.calls) == 1

    def test_approval_event_reaches_the_stream(self, store, monkeypatch):
        monkeypatch.setattr(
            "sage.core.orchestrator.approval_registry", ApprovalRegistry()
        )
        orchestrator = build(FakeHermes(approval_first=True), store)

        events = list(orchestrator.run_stream("delete the thing"))

        assert any(e.get("approval") for e in events)
        assert events[-1]["status"] == JobStatus.NEEDS_APPROVAL.value

    def test_approved_replay_uses_yolo(self, store, monkeypatch):
        registry = ApprovalRegistry()
        monkeypatch.setattr("sage.core.orchestrator.approval_registry", registry)
        hermes = FakeHermes(approval_first=True)
        orchestrator = build(hermes, store)

        job = orchestrator.run("delete the thing")
        pending = registry.resolve(job.approval.id)

        replay = orchestrator.run(pending.task, yolo=True)

        assert replay.status is JobStatus.COMPLETED
        assert hermes.calls[-1]["yolo"] is True

    def test_one_approval_cannot_be_replayed_twice(self, store, monkeypatch):
        registry = ApprovalRegistry()
        monkeypatch.setattr("sage.core.orchestrator.approval_registry", registry)
        orchestrator = build(FakeHermes(approval_first=True), store)

        job = orchestrator.run("delete the thing")

        assert registry.resolve(job.approval.id) is not None
        assert registry.resolve(job.approval.id) is None

    def test_declined_approval_executes_nothing(self, store, monkeypatch):
        registry = ApprovalRegistry()
        monkeypatch.setattr("sage.core.orchestrator.approval_registry", registry)
        hermes = FakeHermes(approval_first=True)
        orchestrator = build(hermes, store)

        job = orchestrator.run("delete the thing")
        registry.resolve(job.approval.id)

        # Nothing re-ran, so the blocked action stays blocked.
        assert len(hermes.calls) == 1

    def test_hermes_failure_is_reported_not_raised(self, store):
        hermes = FakeHermes()
        hermes.fail = RuntimeError("hermes exploded")
        orchestrator = build(hermes, store)

        job = orchestrator.run("x")

        assert job.status is JobStatus.FAILED
        assert "hermes exploded" in job.error

    def test_failure_is_recorded_as_an_outcome(self, store):
        hermes = FakeHermes()
        hermes.fail = RuntimeError("nope")
        build(hermes, store).run("x")

        outcomes = store.recent_route_outcomes(1)
        assert outcomes and outcomes[0]["handler"] == "hermes"
        assert outcomes[0]["success"] == 0


class TestAbort:
    """A failure must still produce an error and a done frame.

    ``return self._abort(...)`` inside a generator returns the generator
    object instead of yielding from it, so the stream ended with no events at
    all and the caller saw nothing but StopIteration.
    """

    def test_analyzer_failure_emits_error_and_done(self, store):
        class Exploding:
            def analyze(self, message):
                raise RuntimeError("analyzer died")

        orchestrator = Orchestrator(
            analyzer=Exploding(),
            handler=object(),
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )

        events = list(orchestrator.run_stream("x"))

        assert any("analyzer died" in e.get("error", "") for e in events)
        assert events[-1]["done"] is True
        assert events[-1]["status"] == JobStatus.FAILED.value

    def test_run_returns_the_failed_job(self, store):
        class Exploding:
            def analyze(self, message):
                raise RuntimeError("nope")

        job = Orchestrator(
            analyzer=Exploding(),
            handler=object(),
            hermes=FakeHermes(),
            verifier=Verifier(),
        ).run("x")

        assert job is not None
        assert job.status is JobStatus.FAILED


class TestOutcomeFeedback:
    """Recorded outcomes must reach the analyzer and reflect real answers."""

    def test_sage_success_is_recorded(self, store):
        build(FakeHermes(), store, fix_analysis=analysis(delegate=False, tools=()))

        class StubHandler:
            def run(self, *a, **k):
                yield {"type": "text", "delta": "Darwin"}
                yield {"type": "done"}

            def retrieve(self, message):
                return None, None

        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False, tools=())),
            handler=StubHandler(),
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )
        orchestrator.run("what am I on")

        outcomes = store.recent_route_outcomes(1)
        assert outcomes[0]["handler"] == "sage"
        assert outcomes[0]["success"] == 1

    def test_refusal_is_recorded_as_a_failure(self, store):
        class Refusing:
            def run(self, *a, **k):
                yield {"type": "text", "delta": "I can't help with that."}
                yield {"type": "done"}

            def retrieve(self, message):
                return None, None

        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False, tools=())),
            handler=Refusing(),
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )
        job = orchestrator.run("do something forbidden")

        assert job.status is JobStatus.FAILED
        assert store.recent_route_outcomes(1)[0]["success"] == 0

    def test_analyzer_prompt_carries_the_tally(self, store):
        from sage.core.job_analyzer import Analyzer

        db = Database(":memory:")
        db.initialize()
        db.record_route_outcome(1, "hermes", "shell", "terminal", True, 10)
        db.record_route_outcome(1, "hermes", "shell", "terminal", False, 10)

        captured = {}

        class SpyLLM:
            def chat(self, messages, system=None):
                captured["prompt"] = messages[0]["content"]
                return '{"intent":"a","action":"b","delegate":false,"tools":[]}'

        Analyzer(llm=SpyLLM(), store=db, conversation_id=1).analyze("x")

        assert "hermes/shell: 1/2 answered" in captured["prompt"]

    def test_no_history_means_no_note(self):
        from sage.core.job_analyzer import Analyzer

        captured = {}

        class SpyLLM:
            def chat(self, messages, system=None):
                captured["prompt"] = messages[0]["content"]
                return '{"intent":"a","action":"b","delegate":false,"tools":[]}'

        Analyzer(llm=SpyLLM()).analyze("x")

        assert "How recent requests" not in captured["prompt"]

    def test_history_read_failure_does_not_break_routing(self):
        from sage.core.job_analyzer import Analyzer

        class BrokenStore:
            def recent_route_outcomes(self, *a, **k):
                raise RuntimeError("db gone")

        class SpyLLM:
            def chat(self, messages, system=None):
                return '{"intent":"a","action":"b","delegate":false,"tools":[]}'

        result = Analyzer(llm=SpyLLM(), store=BrokenStore(), conversation_id=1).analyze("x")

        assert result.delegate is False


class RefusesDelete:
    """Stands in for the tool loop when bash refuses a destructive command."""

    def __init__(self, approved=None):
        self.approved = approved

    def run(self, message, conversation=None, model=None, surface="cli",
            approved_command=None, conversation_id=None):
        self.seen_approval = approved_command
        command = "rm -rf /tmp/sage-victim"
        yield {"type": "tool_pending", "name": "bash", "arguments": {}}
        yield {
            "type": "tool",
            "name": "bash",
            "arguments": {},
            "result": {"error": "approval required", "approval_required": True},
        }
        if approved_command is None:
            yield {
                "type": "approval_required",
                "name": "bash",
                "command": command,
                "reason": "recursive delete",
            }


class TestLocalBashApproval:
    """SAGE's own bash used to run destructive commands with no gate at all."""

    def test_destructive_command_parks_the_job(self, store):
        handler = RefusesDelete()
        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False, tools=())),
            handler=handler,
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )

        events = list(orchestrator.run_stream("delete /tmp/sage-victim"))
        job = events[-1]["job"]

        assert job.status is JobStatus.NEEDS_APPROVAL
        assert job.approval.approved_command == "rm -rf /tmp/sage-victim"
        assert any(event.get("approval") for event in events)

    def test_parked_job_records_no_route_outcome(self, store):
        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False, tools=())),
            handler=RefusesDelete(),
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )

        orchestrator.run("delete /tmp/sage-victim")

        # Nothing completed, so there is no success or failure to report. A
        # failure here would teach the analyzer the route was bad.
        assert store.recent_route_outcomes(1) == []

    def test_approved_replay_passes_the_command(self, store):
        handler = RefusesDelete()
        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False, tools=())),
            handler=handler,
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )

        job = orchestrator.run("delete /tmp/sage-victim")
        orchestrator.run(
            job.approval.task,
            yolo=True,
            approved_command=job.approval.approved_command,
        )

        assert handler.seen_approval == "rm -rf /tmp/sage-victim"

    def test_approval_never_reaches_hermes(self, store):
        hermes = FakeHermes()
        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False, tools=())),
            handler=RefusesDelete(),
            hermes=hermes,
            verifier=Verifier(),
            store=store,
            conversation_id=1,
        )

        orchestrator.run("delete /tmp/sage-victim")

        assert hermes.calls == []


class TestHermesOutcomeIsRecordedAfterVerification:
    def test_hermes_refusal_is_not_recorded_as_success(self, store):
        class RefusingHermes(FakeHermes):
            def run(self, task, model=None, session_id=None, yolo=False):
                super().run(task, model=model, session_id=session_id, yolo=yolo)
                return "I can't help with that."

        orchestrator = build(RefusingHermes(), store)
        job = orchestrator.run("do a thing")

        # HERMES returned text, but it was a refusal: recorded as a success it
        # would make the analyzer prefer the route that refused.
        assert job.status is JobStatus.FAILED
        assert store.recent_route_outcomes(1)[0]["success"] == 0

    def test_hermes_success_is_recorded(self, store):
        orchestrator = build(FakeHermes(), store)
        job = orchestrator.run("do a thing")

        assert job.status is JobStatus.COMPLETED
        assert store.recent_route_outcomes(1)[0]["handler"] == "hermes"
        assert store.recent_route_outcomes(1)[0]["success"] == 1


class TestNoExecutionSurface:
    """A surface that may not execute must not reach HERMES either.

    HERMES has its own shell. Gating SAGE's tool table while still delegating
    hands out exactly the access the gate refused, and the web surface can be
    bound to a port with no authentication.
    """

    class Handler:
        def __init__(self):
            self.calls = 0

        def run(self, *a, **k):
            self.calls += 1
            yield {"type": "text", "delta": "handled here"}
            yield {"type": "done", "content": "handled here"}

    def _orchestrator(self, hermes, local_tools):
        return Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=True)),
            handler=self.Handler(),
            hermes=hermes,
            verifier=Verifier(),
            surface="web",
            local_tools=local_tools,
        )

    def test_web_without_local_tools_never_delegates(self, store):
        hermes = FakeHermes()
        orchestrator = self._orchestrator(hermes, local_tools=False)

        job = orchestrator.run("do a thing", surface="web")

        assert hermes.calls == []
        assert orchestrator.handler.calls == 1
        assert job.status is JobStatus.COMPLETED

    def test_web_with_local_tools_may_delegate(self, store):
        hermes = FakeHermes()
        orchestrator = self._orchestrator(hermes, local_tools=True)

        orchestrator.run("do a thing", surface="web")

        assert len(hermes.calls) == 1

    def test_cli_always_may_delegate(self, store):
        hermes = FakeHermes()
        orchestrator = Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=True)),
            handler=self.Handler(),
            hermes=hermes,
            verifier=Verifier(),
            surface="cli",
        )

        orchestrator.run("do a thing", surface="cli")

        assert len(hermes.calls) == 1


class TestFabricatedResultRetry:
    """A turn that narrates an action gets one correction, not a failure.

    The model claiming "Terminal opened" without calling a tool is a recoverable
    mistake, so the orchestrator re-runs the turn naming the gap before giving
    up on it.
    """

    class NarratingHandler:
        """First turn narrates, second turn actually calls the tool."""

        def __init__(self):
            self.calls = 0
            self.tasks = []

        def run(self, message, conversation=None, model=None, surface="cli",
                approved_command=None, conversation_id=None):
            self.calls += 1
            self.tasks.append(message)

            if self.calls == 1:
                yield {"type": "text", "delta": "Terminal opened (a new window)."}
                return

            yield {
                "type": "tool", "name": "pc_control",
                "arguments": {"action": "open_app"},
                "result": {"ok": True, "exit_code": 0},
            }
            yield {"type": "text", "delta": "Terminal opened."}

    class StubbornHandler:
        """Narrates both times; the turn must fail rather than pass."""

        def __init__(self):
            self.calls = 0

        def run(self, message, conversation=None, model=None, surface="cli",
                approved_command=None, conversation_id=None):
            self.calls += 1
            yield {"type": "text", "delta": "Terminal opened (a new window)."}

    def _orchestrator(self, handler):
        store = Database(":memory:")
        store.initialize()
        return Orchestrator(
            analyzer=AnalyzerStub(analysis(delegate=False)),
            handler=handler,
            hermes=FakeHermes(),
            verifier=Verifier(),
            store=store,
            conversation_id=store.get_or_create_primary_conversation(),
        )

    def test_one_retry_with_a_correction_reaches_success(self):
        handler = self.NarratingHandler()
        job = self._orchestrator(handler).run("open terminal")

        assert handler.calls == 2
        assert job.status is JobStatus.COMPLETED
        assert job.result == "Terminal opened."
        # The retry has to name the gap, or it is just the same turn again.
        assert "instead of doing it" in handler.tasks[1]

    def test_retry_is_bounded_at_one(self):
        handler = self.StubbornHandler()
        job = self._orchestrator(handler).run("open terminal")

        assert handler.calls == 2
        assert job.status is JobStatus.FAILED
        assert "no action taken" in job.error

    def test_a_tool_that_really_ran_is_never_retried(self):
        class HonestHandler:
            def __init__(self):
                self.calls = 0

            def run(self, message, conversation=None, model=None, surface="cli",
                    approved_command=None, conversation_id=None):
                self.calls += 1
                yield {
                    "type": "tool", "name": "pc_control", "arguments": {},
                    "result": {"ok": True},
                }
                yield {"type": "text", "delta": "Terminal opened."}

        handler = HonestHandler()
        job = self._orchestrator(handler).run("open terminal")

        assert handler.calls == 1
        assert job.status is JobStatus.COMPLETED
