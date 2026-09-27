import json
import re

from sage.core import capabilities
from sage.core.analysis import KNOWN_TOOLS, TaskAnalysis
from sage.core.llm import LLMClient

SYSTEM = (
    "You are a request classifier inside the SAGE assistant. "
    "Reply with a single JSON object and nothing else. "
    "No prose, no explanation, no code fences."
)

def _schema(surface, local_tools):
    return {
        "intent": "one sentence describing what the user actually wants",
        "action": "imperative verb phrase for the work, e.g. 'search the web'",
        "delegate": capabilities.routing_rule(surface, local_tools),
        "target": "the string 'hermes' if delegate is true, otherwise null",
        "tools": (
            "list of tools to run BEFORE answering, chosen from "
            f"{list(KNOWN_TOOLS)}. Use ['web_search'] when the answer depends on "
            "live information. Use [] when you can answer directly or when the "
            "request needs an agent rather than a lookup."
        ),
    }


def _brief(surface, local_tools):
    return (
        "Two agents are available.\n\n"
        f"{capabilities.hermes_prompt()}\n\n"
        f"{capabilities.sage_prompt(surface, local_tools)}\n"
    )

REQUIRED = ("intent", "action", "delegate", "tools")

ATTEMPTS = 2


class Analyzer:
    def __init__(self, llm=None, surface="cli", local_tools=True, store=None,
                 conversation_id=None):
        self.llm = llm or LLMClient()
        self.surface = surface
        self.local_tools = local_tools
        self.store = store
        self.conversation_id = conversation_id

    def _history_note(self):
        """Summarise how recent routes turned out, so the model can learn.

        A verifier that only checks for a non-empty string called almost every
        run a success, which made the recorded outcomes worthless as feedback.
        They now reflect refused, errored, and empty answers too, so a handler
        that keeps failing here is visible to the model on the next turn.
        """
        if not self.store or not self.conversation_id:
            return ""

        try:
            outcomes = self.store.recent_route_outcomes(self.conversation_id, limit=10)
        except Exception as error:
            print(f"[ANALYZER] could not read route history: {error}")
            return ""

        if not outcomes:
            return ""

        tally = {}

        for row in outcomes:
            key = f"{row['handler']}/{row['action'] or 'unknown'}"
            passed, total = tally.get(key, (0, 0))
            tally[key] = (passed + bool(row["success"]), total + 1)

        lines = [
            f"  {key}: {passed}/{total} answered"
            for key, (passed, total) in sorted(tally.items())
        ]

        return (
            "How recent requests in this conversation turned out "
            "(prefer a handler that has been answering):\n" + "\n".join(lines) + "\n"
        )

    def analyze(self, message):
        prompt = (
            f"{_brief(self.surface, self.local_tools)}\n"
            f"{self._history_note()}"
            "Classify the request below.\n\n"
            f"Expected JSON shape:\n{json.dumps(_schema(self.surface, self.local_tools), indent=2)}\n\n"
            f"Request:\n{message}\n"
        )

        last_error = None

        for attempt in range(ATTEMPTS):
            try:
                raw = self.llm.chat(
                    [{"role": "user", "content": prompt}],
                    system=SYSTEM,
                )

                return self._parse(raw)
            except (ValueError, TypeError) as error:
                last_error = error
                print(f"[ANALYZER] Bad JSON (attempt {attempt + 1}): {error}")

        # Fail closed: a malformed analysis must never trigger a subprocess
        # or an unrequested network call.
        print("[ANALYZER] Falling back to direct handling.")
        return TaskAnalysis(intent=message, action=message, delegate=False, target=None, tools=[])

    def _parse(self, raw):
        data = self._coerce(self._loads_object(raw))

        return TaskAnalysis(
            intent=data["intent"],
            action=data["action"],
            delegate=data["delegate"],
            target=data["target"],
            tools=data["tools"],
        )

    @staticmethod
    def _loads_object(raw):
        if not isinstance(raw, str):
            raise ValueError(f"expected text, got {type(raw).__name__}")

        text = re.sub(r"\A```(?:json)?|```\Z", "", raw.strip(), flags=re.I | re.S).strip()

        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end <= start:
            raise ValueError("no JSON object found in response")

        snippet = text[start : end + 1]

        try:
            data = json.loads(snippet)
        except json.JSONDecodeError as exc:
            # The model often brackets valid JSON with prose, which lands here.
            # Report where it broke instead of leaking a bare decoder message.
            raise ValueError(
                f"response looked like JSON but did not parse at column {exc.colno}: {exc.msg}"
            ) from None

        if not isinstance(data, dict):
            raise ValueError("JSON payload is not an object")

        return data

    @staticmethod
    def _coerce(data):
        missing = [key for key in REQUIRED if key not in data]

        if missing:
            raise ValueError(f"missing keys: {', '.join(missing)}")

        delegate = data["delegate"]

        if isinstance(delegate, str):
            delegate = delegate.strip().lower() in ("true", "yes", "1")

        delegate = bool(delegate)
        target = data.get("target") or ("hermes" if delegate else None)

        requested = data.get("tools") or []

        if isinstance(requested, str):
            requested = [requested]

        if not isinstance(requested, list):
            raise ValueError("tools must be a list")

        # Keep only tools we actually implement, in a stable order.
        named = {str(name).strip().lower() for name in requested}
        tools = [name for name in KNOWN_TOOLS if name in named]

        return {
            "intent": str(data["intent"]).strip(),
            "action": str(data["action"]).strip(),
            "delegate": delegate,
            "target": str(target) if target else None,
            "tools": tools,
        }
