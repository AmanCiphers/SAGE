"""Tool-call stream assembly, result summarising, and the search chain.

The LLM parts are faked: the point is to pin the reassembly logic, which is
where a silent bug would drop or corrupt a tool call.
"""

import json

import pytest

from sage.core.orchestrator import _summarize
from sage.tools import registry
from sage.tools import search as search_module
from sage.tools import tavily
from sage.tools.bash import run_bash
from sage.tools.llm_helpers import assemble_tool_calls
from sage.tools.loop import run_tool_loop
from sage.tools.search import search


class FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = type("F", (), {"name": name, "arguments": arguments})()


def chunks(*deltas):
    return [type("C", (), {"choices": [type("X", (), {"delta": d})()]})() for d in deltas]


class TestToolCallAssembly:
    def test_arguments_split_across_chunks_are_joined(self):
        # The real failure mode: arguments arrive fragmented and naive handling
        # drops all but the last piece, producing invalid JSON.
        deltas = [
            FakeDelta(tool_calls=[FakeCall(0, id="c1", name="web_search", arguments='{"que')]),
            FakeDelta(tool_calls=[FakeCall(0, arguments='ry": "x"}')]),
        ]

        calls = assemble_tool_calls(deltas)

        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "web_search"
        assert json.loads(calls[0]["function"]["arguments"]) == {"query": "x"}

    def test_parallel_calls_keep_their_own_arguments(self):
        deltas = [
            FakeDelta(tool_calls=[
                FakeCall(0, id="a", name="bash", arguments='{"command":'),
                FakeCall(1, id="b", name="web_search", arguments='{"query":'),
            ]),
            FakeDelta(tool_calls=[
                FakeCall(0, arguments='"ls"}'),
                FakeCall(1, arguments='"q"}'),
            ]),
        ]

        calls = assemble_tool_calls(deltas)

        assert [c["function"]["name"] for c in calls] == ["bash", "web_search"]
        assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}
        assert json.loads(calls[1]["function"]["arguments"]) == {"query": "q"}

    def test_content_only_stream_has_no_tool_calls(self):
        assert assemble_tool_calls([FakeDelta(content="hi")]) == []

    def test_missing_index_is_tolerated(self):
        deltas = [FakeDelta(tool_calls=[FakeCall(None, id="a", name="bash", arguments="{}")])]
        calls = assemble_tool_calls(deltas)
        assert calls[0]["function"]["name"] == "bash"


class TestSummarize:
    @pytest.mark.parametrize(
        "result,expected",
        [
            ({"error": "boom"}, "boom"),
            ({"output": "hello"}, "hello"),
            ({"results": [1, 2, 3]}, "results: 3 item(s)"),
            ({"tasks": []}, "tasks: 0 item(s)"),
            ({"note": "scheduled"}, "scheduled"),
            ({"status": 200, "text": "x" * 5000}, "HTTP 200"),
            ({"text": "abc"}, "3 chars of text"),
            ("plain", "plain"),
        ],
    )
    def test_one_line_summaries(self, result, expected):
        assert _summarize(result) == expected

    def test_truncates(self):
        assert len(_summarize("y" * 5000)) <= 220

    def test_never_raises(self):
        for value in [None, 0, [], {}, {"a": {"b": {"c": 1}}}]:
            _summarize(value)


class TestSearchChain:
    def test_prefers_tavily(self, monkeypatch):
        monkeypatch.setattr(tavily, "available", lambda: True)
        monkeypatch.setattr(
            search_module.tavily,
            "search",
            lambda q, num_results=5: {"query": q, "provider": "tavily", "results": [
                {"title": "t", "url": "u", "snippet": "s"}
            ]},
        )

        found = search("q")

        assert found["provider"] == "tavily"
        assert found["attempts"] == []

    def test_falls_back_to_duckduckgo(self, monkeypatch):
        def unavailable(query, num_results=5):
            raise tavily.TavilyUnavailable("key rejected (HTTP 401)")

        monkeypatch.setattr(tavily, "available", lambda: True)
        monkeypatch.setattr(search_module.tavily, "search", unavailable)
        monkeypatch.setattr(
            search_module,
            "web_search",
            lambda q, num_results=5: {"query": q, "provider": "duckduckgo", "results": [
                {"title": "t", "url": "u", "snippet": "s"}
            ]},
        )

        found = search("q")

        assert found["provider"] == "duckduckgo"
        assert "tavily" in found["attempts"][0]

    def test_raises_only_when_everything_fails(self, monkeypatch):
        monkeypatch.setattr(tavily, "available", lambda: False)
        monkeypatch.setattr(
            search_module,
            "web_search",
            lambda q, num_results=5: (_ for _ in ()).throw(RuntimeError("blocked")),
        )

        with pytest.raises(RuntimeError, match="all search providers failed"):
            search("q")

    def test_empty_results_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(tavily, "available", lambda: True)
        monkeypatch.setattr(
            search_module.tavily,
            "search",
            lambda q, num_results=5: {"query": q, "provider": "tavily", "results": []},
        )
        monkeypatch.setattr(
            search_module,
            "web_search",
            lambda q, num_results=5: {"query": q, "provider": "duckduckgo", "results": []},
        )

        with pytest.raises(RuntimeError, match="no usable results"):
            search("q")

    def test_raw_text_included_only_when_requested(self, monkeypatch):
        monkeypatch.setattr(tavily, "available", lambda: True)
        monkeypatch.setattr(
            search_module.tavily,
            "search",
            lambda q, num_results=5: {"query": q, "provider": "tavily", "results": [
                {"title": "t", "url": "u", "snippet": "s", "text": "body"}
            ]},
        )

        assert "text" in search("q", include_raw=True)["results"][0]
        assert "text" not in search("q", include_raw=False)["results"][0]


class ScriptedLLM:
    """Replays a fixed list of model turns, so the loop can be driven offline."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def stream_events(self, messages, model=None, system=None, tools=None):
        turn = self.turns[min(self.calls.__len__(), len(self.turns) - 1)]
        self.calls.append(messages)
        yield {"type": "tool_call", "name": turn["name"], "arguments": turn["args"]}
        yield {
            "type": "done",
            "message": {
                "role": "assistant",
                "content": turn.get("content", ""),
                "tool_calls": [
                    {
                        "id": f"c{self.calls.__len__()}",
                        "function": {"name": turn["name"], "arguments": turn["args"]},
                    }
                ],
            },
        }


def bash_turn(command, content=""):
    return {"name": "bash", "args": json.dumps({"command": command}), "content": content}


ACTIONS = {"bash": run_bash}


@pytest.fixture
def local_tools_on():
    # bash is a local tool, and the gate is process-wide.
    original = registry.local_tools_allowed()
    registry.set_local_tools_allowed(True)
    yield
    registry.set_local_tools_allowed(original)


class TestDestructiveRoundTrip:
    """Refuse, ask, and only run the exact command that was shown."""

    @pytest.fixture(autouse=True)
    def _gate(self, local_tools_on):
        pass

    def _victim(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("important")
        return victim

    def test_refused_then_approved_actually_runs_it(self, tmp_path):
        victim = self._victim(tmp_path)
        command = f"rm -rf {victim}"
        llm = ScriptedLLM([bash_turn(command, "done")])

        events = list(run_tool_loop(llm, "delete it", actions=ACTIONS))

        assert any(e["type"] == "approval_required" for e in events)
        assert victim.exists(), "must not run before approval"

        events = list(
            run_tool_loop(llm, "delete it", actions=ACTIONS, approved_command=command)
        )

        assert not any(e["type"] == "approval_required" for e in events)
        assert not victim.exists(), "approved command should have run"

    def test_a_widened_command_is_parked_again(self, tmp_path):
        victim = self._victim(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        approved = f"rm -rf {victim}"
        widened = f"{approved} && rm -rf {other}"

        llm = ScriptedLLM([bash_turn(widened, "done")])

        events = list(
            run_tool_loop(llm, "delete both", actions=ACTIONS, approved_command=approved)
        )

        assert any(e["type"] == "approval_required" for e in events)
        assert victim.exists() and other.exists()

    def test_no_retry_loop_when_refused(self, tmp_path):
        # The model must not be able to try variants until one slips through.
        victim = self._victim(tmp_path)
        llm = ScriptedLLM([bash_turn(f"rm -rf {victim}", "done")])

        list(run_tool_loop(llm, "delete it", actions=ACTIONS))

        assert len(llm.calls) == 1
