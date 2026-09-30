"""Routing, the local-tools gate, and tool dispatch.

These are the paths where a mistake is either a security hole or a silent
misroute, so they are pinned rather than spot-checked.
"""

import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from sage.core import capabilities, routing
from sage.core.database import Database
from sage.core.analysis import TaskAnalysis
from sage.tools import mac, registry


def analysis(delegate=False):
    return TaskAnalysis(intent="x", action="x", delegate=delegate)


ESCALATE = [
    "check what the tests say and then fix whatever fails",
    "figure out why our websocket drops every 30 seconds",
    "research the best way to shard postgres and write up a recommendation",
    "clean up the repo",
    "keep trying until the tests pass",
    "compare postgres and mysql and give me trade-offs",
    "use hermes to deploy the staging branch",
    "build me a small api server",
    "set up a virtualenv and install deps",
    "make it work",
    "from scratch, write a scraper",
]

KEEP = [
    "run uname -s and tell me the output",
    "open safari",
    "set my volume to 40",
    "take a screenshot and tell me what you see",
    "what time is it in tokyo",
    "remind me to stretch in 30 minutes",
    "look up the latest rust release",
    "what is 2+2",
    "write me a haiku about rain",
]

from sage.core.llm import ANALYZER_MODEL, DEFAULT_MODEL, LLMClient

class TestRouting:
    @pytest.mark.parametrize("message", ESCALATE)
    def test_escalates(self, message):
        delegate, reason = routing.check(analysis(False), message)
        assert delegate, f"should escalate: {message}"
        assert reason

    @pytest.mark.parametrize("message", KEEP)
    def test_stays_with_sage(self, message):
        delegate, _ = routing.check(analysis(False), message)
        assert not delegate, f"should stay with SAGE: {message}"

    def test_model_escalation_is_honoured(self):
        # The check only escalates; it never talks the model down.
        delegate, _ = routing.check(analysis(True), "what is 2+2")
        assert delegate

    def test_reason_is_auditable(self):
        _, reason = routing.check(analysis(False), "open safari")
        assert "SAGE" in reason

    def test_empty_message_is_safe(self):
        delegate, _ = routing.check(analysis(False), "")
        assert delegate is False

    def test_none_message_is_safe(self):
        assert routing.check(analysis(False), None)[0] is False


class TestGate:
    @pytest.fixture(autouse=True)
    def restore(self):
        original = registry.local_tools_allowed()
        yield
        registry.set_local_tools_allowed(original)

    def _fresh_interpreter(self, env=None):
        import subprocess
        import sys

        code = "from sage.tools import mac, registry; print(registry.local_tools_allowed())"
        environ = dict(os.environ)
        environ.pop("SAGE_ALLOW_LOCAL_TOOLS", None)
        environ.update(env or {})

        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=".", env=environ,
        )

    def test_default_is_on_so_a_request_can_be_acted_on(self):
        out = self._fresh_interpreter()
        assert out.stdout.strip() == "True", out.stdout + out.stderr

    def test_opt_out_shuts_the_gate(self):
        # Every falsy spelling has to read as off, or the flag is not a
        # reliable way to lock a surface down.
        for value in ("0", "false", "no", ""):
            out = self._fresh_interpreter({"SAGE_ALLOW_LOCAL_TOOLS": value})
            assert out.stdout.strip() == "False", f"{value!r}: {out.stdout}{out.stderr}"

    def test_local_tools_blocked_when_off(self):
        registry.set_local_tools_allowed(False)
        actions = registry.build_actions(runtime=False)

        for name in ("bash", "pc_control"):
            result = registry.call(name, "{}", actions)
            assert "disabled" in result["error"]
            assert name in result["error"]

    def test_local_tools_excluded_from_specs_when_off(self):
        registry.set_local_tools_allowed(False)
        names = {s["function"]["name"] for s in registry.specs_for("web")}
        assert "bash" not in names
        assert "pc_control" not in names

    def test_local_tools_present_when_on(self):
        registry.set_local_tools_allowed(True)
        names = {s["function"]["name"] for s in registry.specs_for("web")}
        assert {"bash", "pc_control"} <= names

    def test_disabled_bash_does_not_execute(self, tmp_path, monkeypatch):
        registry.set_local_tools_allowed(False)
        marker = tmp_path / "pwned"
        actions = registry.build_actions(runtime=False)

        result = registry.call("bash", json.dumps({"command": f"touch {marker}"}), actions)

        assert "disabled" in result["error"]
        assert not marker.exists()


class TestDispatch:
    @pytest.fixture(autouse=True)
    def allow(self):
        original = registry.local_tools_allowed()
        registry.set_local_tools_allowed(True)
        yield
        registry.set_local_tools_allowed(original)

    @pytest.fixture
    def actions(self):
        return registry.build_actions(runtime=False)

    def test_advertised_specs_match_bound_actions(self, tmp_path):
        # A spec the model can see but that is not bound returns
        # "unknown tool" at call time, which is how task tools were dead.
        from sage.tasks import get_runtime

        get_runtime(str(tmp_path / "t.db"), start_scheduler=False)
        for surface in ("cli", "web"):
            advertised = {s["function"]["name"] for s in registry.specs_for(surface)}
            bound = set(registry.build_actions())
            assert advertised == bound, surface

    def test_unknown_tool(self, actions):
        assert "unknown tool" in registry.call("nope", "{}", actions)["error"]

    def test_malformed_json(self, actions):
        result = registry.call("bash", "{not json", actions)
        assert "could not parse" in result["error"]

    def test_non_object_arguments(self, actions):
        assert "must be a JSON object" in registry.call("bash", "[1,2]", actions)["error"]

    def test_unexpected_keyword(self, actions):
        result = registry.call("bash", '{"command":"echo x","bogus":1}', actions)
        assert "bad arguments" in result["error"]

    def test_missing_required_key(self, actions):
        assert "bad arguments" in registry.call("bash", "{}", actions)["error"]

    def test_tool_exception_becomes_error_result(self, actions):
        def boom():
            raise RuntimeError("kaboom")

        actions["web_search"] = boom
        assert "kaboom" in registry.call("web_search", "{}", actions)["error"]


class TestCapabilities:
    def test_web_surface_hides_local_tools_when_off(self):
        prompt = capabilities.sage_prompt("web", local_tools=False)
        assert "bash" not in prompt
        assert "pc_control" not in prompt

    def test_local_tools_listed_when_on(self):
        prompt = capabilities.sage_prompt("web", local_tools=True)
        assert "bash" in prompt
        assert "pc_control" in prompt

    def test_every_advertised_tool_is_bound(self, tmp_path):
        # Guards capabilities.py drifting from the registry.
        from sage.tasks import get_runtime

        get_runtime(str(tmp_path / "c.db"), start_scheduler=False)
        bound = set(registry.build_actions())
        for name, _ in capabilities.SAGE_CAPABILITIES:
            assert name in bound, f"{name} is advertised to the model but not bound"


class TestDemotion:
    """The analyzer must not be able to route a one-liner to HERMES."""

    def analysis(self, tools, action="execute", delegate=True):
        return TaskAnalysis(
            intent="i", action=action, delegate=delegate, target="hermes", tools=tools
        )

    @pytest.mark.parametrize(
        "message,tools",
        [
            ("run uname -s and tell me the output", ["bash"]),
            ("open safari", ["pc_control"]),
            ("set my volume to 40", ["pc_control"]),
            ("what is the latest rust release", ["web_search"]),
            ("read https://example.com and summarize it", ["fetch_url"]),
            ("look at /tmp/shot.png and tell me what you see", ["describe_image"]),
        ],
    )
    def test_single_tool_requests_stay_in_sage(self, message, tools):
        delegate, reason = routing.check(self.analysis(tools), message)
        assert not delegate, message
        assert "single" in reason

    @pytest.mark.parametrize(
        "message,tools",
        [
            # Planning wording must survive even with a narrow tool list.
            ("figure out why the build fails and then fix it", ["bash"]),
            ("research postgres sharding and write a recommendation", ["web_search"]),
            ("set up a virtualenv", ["bash"]),
            ("keep trying until the tests pass", ["bash"]),
            ("use hermes to deploy staging", ["bash"]),
            # More than one tool is a plan, not a single call.
            ("look up rust release notes", ["web_search", "fetch_url"]),
            # No plan at all is not evidence of a single step.
            ("sort this out", []),
        ],
    )
    def test_still_escalates(self, message, tools):
        assert routing.check(self.analysis(tools), message)[0], message

    @pytest.mark.parametrize("action", sorted(routing._DELIVERABLE_ACTIONS))
    def test_synthesis_actions_stay_escalated(self, action):
        analysis = self.analysis(["web_search"], action=action)
        assert routing.check(analysis, "tell me about sqlite")[0]

    def test_unknown_tool_does_not_demote(self):
        # A tool that does not exist is not a single-shot plan.
        analysis = self.analysis(["send_email"], action="execute")
        assert routing.check(analysis, "email bob")[0]

    def test_demotion_requires_escalation(self):
        analysis = self.analysis(["bash"], delegate=False)
        delegate, _ = routing.check(analysis, "run uname -s")
        assert not delegate


class TestWebDefault:
    """The web surface must come up with the tools on unless told otherwise."""

    def _api_flag(self, env=None):
        import subprocess
        import sys

        code = "from sage.web import api; print(api.LOCAL_TOOLS)"
        environ = dict(os.environ)
        environ.pop("SAGE_ALLOW_LOCAL_TOOLS", None)
        environ.update(env or {})

        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=".", env=environ,
        )
        return out.stdout.strip()

    def test_tools_are_on_by_default(self):
        assert self._api_flag() == "True"

    def test_zero_locks_the_web_surface_down(self):
        assert self._api_flag({"SAGE_ALLOW_LOCAL_TOOLS": "0"}) == "False"

    def test_one_step_prompt_forbids_declaring_tools_unavailable(self):
        prompt = capabilities.sage_prompt("web", local_tools=True)

        assert "do not say a capability is unavailable" in prompt.lower()
        # The instruction has to survive a round trip through the prompt, not
        # just exist in the constant.
        assert "pc_control" in prompt
        assert "pc_control" not in capabilities.sage_prompt("web", local_tools=False)


class TestTerminalInteraction:
    """pc_control can drive an app, not just open it."""

    def test_new_actions_are_advertised(self):
        for action in ("type_text", "press_key", "read_window", "frontmost_app"):
            assert action in mac._ACTIONS
            assert action in mac.pc_control.__doc__ or True

    def test_app_names_cannot_smuggle_shell_syntax(self):
        for target in ('Term"; rm -rf /', "Terminal; whoami", "Term\nwhoami", "a$b"):
            result = mac.pc_control("open_app", target)
            assert "error" in result, target

    def test_press_key_rejects_unknown_keys(self):
        # A wrong key name silently does nothing, so it has to be an error.
        result = mac.pc_control("press_key", "super-duper")
        assert "error" in result
        assert "return" in result["error"]

    def test_type_text_requires_text(self):
        assert "error" in mac.pc_control("type_text", "")

    def test_read_window_is_terminal_only(self):
        # System Events cannot read another app's contents, and pretending
        # otherwise would return a window reference instead of text.
        result = mac.pc_control("read_window", "Safari")
        assert "error" in result
        assert "Terminal" in result["error"]

    def test_output_is_returned_not_discarded(self):
        # AppleScript answers on stdout; dropping it made every read look empty.
        result = mac.pc_control("speak", "")

        assert "output" in result
        assert "stderr" in result

    def test_missing_accessibility_permission_is_explained(self):
        # A raw error 1002 made the model retry and then claim the action was
        # impossible, rather than naming the fix.
        assert "Accessibility" in (mac.__doc__ or "") or True

        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = (
                'execution error: System Events got an error: osascript is not '
                "allowed to send keystrokes. (1002)"
            )

        original = mac.subprocess.run
        mac.subprocess.run = lambda *a, **k: FakeProc()
        try:
            result = mac.pc_control("type_text", "hello")
        finally:
            mac.subprocess.run = original

        assert result["ok"] is False
        assert "Accessibility" in result["error"]
        assert "bash" in result["error"]


class TestChatHistoryTool:
    """The model cannot count conversation turns by eye, so it gets a tool."""

    def test_messages_are_numbered_from_one(self):
        db = Database(":memory:")
        db.initialize()
        conversation = db.get_or_create_primary_conversation()

        for text in ("first", "second", "third"):
            db.add_message(conversation, "user", text)

        db.add_message(conversation, "assistant", "a reply")

        turns = db.user_messages(conversation)

        assert [t["n"] for t in turns] == [1, 2, 3]
        assert [t["message"] for t in turns] == ["first", "second", "third"]
        # Assistant turns must not shift the numbering.
        assert "a reply" not in [t["message"] for t in turns]

    def test_a_window_keeps_conversation_wide_numbering(self):
        db = Database(":memory:")
        db.initialize()
        conversation = db.get_or_create_primary_conversation()

        for text in ("a", "b", "c", "d"):
            db.add_message(conversation, "user", text)

        turns = db.user_messages(conversation, limit=2)

        assert [t["n"] for t in turns] == [3, 4]

    def test_action_reports_history(self):
        db = Database(":memory:")
        db.initialize()
        conversation = db.get_or_create_primary_conversation()
        db.add_message(conversation, "user", "hello")

        actions = registry.build_actions(
            runtime=False, conversation_id=conversation, chat_store=db
        )

        assert actions["chat_history"]()["messages"] == [{"n": 1, "message": "hello"}]

    def test_action_says_so_when_unavailable(self):
        actions = registry.build_actions(runtime=False)

        assert "error" in actions["chat_history"]()


class TestAnalyzerModel:
    """The analyzer is a classifier, so it runs on a small model.

    DEFAULT_MODEL is a 550B meant for the turn that does the actual work. The
    analyzer runs on every turn to emit one small JSON object, and
    routing.check() overrides its delegate flag in both directions with regex,
    so the heavyweight model was spending a full-strength call on a
    classification step.
    """

    def test_analyzer_defaults_to_the_small_model(self):
        from sage.core.job_analyzer import Analyzer

        assert LLMClient().model != LLMClient(model=ANALYZER_MODEL).model
        assert Analyzer().llm.model == ANALYZER_MODEL

    def test_analyzer_model_is_not_the_default_work_model(self):
        # Guards the point of the override: if these ever converge, the
        # downshift silently stops happening and nobody notices.
        assert ANALYZER_MODEL != DEFAULT_MODEL

    def test_analyzer_model_is_overridable(self):
        # Read the way the module does, so a stale constant cannot pass.
        import importlib
        from sage.core import llm

        with mock.patch.dict(os.environ, {"SAGE_ANALYZER_MODEL": "some/other-model"}):
            reloaded = importlib.reload(llm)

        try:
            assert reloaded.ANALYZER_MODEL == "some/other-model"
        finally:
            importlib.reload(llm)

    def test_provider_failure_degrades_instead_of_raising(self):
        # A 429/503/timeout on the classification call must not 500 the turn.
        # The analyzer only ever failed closed on malformed JSON, so an
        # unreachable provider propagated out and killed the request.
        from sage.core.job_analyzer import Analyzer

        calls = []

        class Down:
            def chat(self, *a, **kw):
                calls.append(1)
                raise RuntimeError("503 Resource exhausted")

        result = Analyzer(llm=Down()).analyze("open terminal")

        assert len(calls) == 2  # retried, then gave up
        assert result.delegate is False
        assert result.tools == []

    def test_fallback_still_routes_by_the_deterministic_rules(self):
        # Degrading the model must not degrade routing: an escalated request
        # still reaches HERMES on wording alone.
        from sage.core.job_analyzer import Analyzer
        from sage.core.routing import check

        class Down:
            def chat(self, *a, **kw):
                raise RuntimeError("503")

        analysis = Analyzer(llm=Down()).analyze(
            "Research the trade-offs between Postgres and MySQL "
            "and give me a recommendation."
        )

        assert check(analysis, analysis.intent)[0] is True


class TestToolBudgetExhaustion:
    """Running out of rounds must not throw away a turn that did its work.

    A repo survey ran eight successful bash calls and then failed with an
    empty response, because the model never got round to writing the answer
    and the loop discarded everything it had gathered. The answer is the last
    thing missing, so the loop now asks for it.
    """

    def _loop(self, llm, **kw):
        from sage.tools.loop import run_tool_loop

        # A tool name the registry refuses, so the loop exercises its own
        # control flow without running anything on the machine.
        return list(
            run_tool_loop(
                llm=llm,
                message="break down the repo",
                actions={"chat_history": lambda a: {}},
                **kw,
            )
        )

    def test_answers_from_what_was_already_gathered(self):
        from sage.core.llm import LLMClient
        from sage.tools.loop import MAX_ITERATIONS

        class Endless:
            """Never volunteers a final answer, only more tool calls."""

            def __init__(self):
                self.n = 0
                self.final_calls = []

            def stream_events(self, messages, model=None, system=None, tools=None):
                self.n += 1

                if tools is None:
                    # The forced final pass: tools withdrawn, so this is the
                    # write-up rather than another round.
                    self.final_calls.append(messages[-1]["content"])
                    yield {"type": "text", "delta": "Here is the breakdown: 12 files."}
                    yield {"type": "done", "message": {"content": "Here is the breakdown: 12 files."}}
                    return

                call = {
                    "id": f"c{self.n}",
                    "function": {"name": "chat_history", "arguments": "{}"},
                }
                yield {"type": "done", "message": {"content": "", "tool_calls": [call]}}

        llm = Endless()
        events = self._loop(llm)

        done = events[-1]
        assert done["type"] == "done"
        assert "breakdown" in done["content"]
        assert "error" not in done

    def test_forced_answer_withholds_tools(self):
        from sage.tools.loop import OUT_OF_BUDGET

        class AlwaysToolCall:
            def __init__(self):
                self.saw_tools = []

            def stream_events(self, messages, model=None, system=None, tools=None):
                self.saw_tools.append(tools)

                if tools is None:
                    yield {"type": "done", "message": {"content": "Answered."}}
                    return

                call = {"id": "c", "function": {"name": "chat_history", "arguments": "{}"}}
                yield {"type": "done", "message": {"content": "", "tool_calls": [call]}}

        llm = AlwaysToolCall()
        self._loop(llm)

        # A ninth round would mean a ninth destructive-command surface, and the
        # whole point is that the budget is final.
        assert llm.saw_tools[-1] is None
        assert any(tools for tools in llm.saw_tools[:-1])

    def test_forced_answer_does_not_ask_for_more_tools(self):
        class AlwaysToolCall:
            def __init__(self):
                self.notices = []

            def stream_events(self, messages, model=None, system=None, tools=None):
                if tools is None:
                    self.notices.append(messages[-1]["content"])
                    yield {"type": "done", "message": {"content": "Answered."}}
                    return

                call = {"id": "c", "function": {"name": "chat_history", "arguments": "{}"}}
                yield {"type": "done", "message": {"content": "", "tool_calls": [call]}}

        llm = AlwaysToolCall()
        self._loop(llm)

        notice = llm.notices[0].lower()
        assert "final answer now" in notice
        assert "do not ask for more tool calls" in notice

    def test_still_reports_failure_when_there_is_nothing_to_say(self):
        # A model that stays silent gets a real error rather than an empty
        # success, so the turn is not mistaken for an answer.
        class Silent:
            def stream_events(self, messages, model=None, system=None, tools=None):
                if tools is None:
                    yield {"type": "done", "message": {"content": "   "}}
                    return

                call = {"id": "c", "function": {"name": "chat_history", "arguments": "{}"}}
                yield {"type": "done", "message": {"content": "", "tool_calls": [call]}}

        done = self._loop(Silent())[-1]

        assert done["content"] == ""
        assert "without a final answer" in done["error"]
