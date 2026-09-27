"""Capability check applied after the model proposes a route.

The analyzer is a good judge of intent but it is lenient: it tends to wave
through work that actually needs planning, and it used to wave the other way
and hand simple single commands to a heavyweight agent. This module is the
deterministic backstop. It never sends work to Hermes on its own; it only
overrides a "false" the model produced, so the model stays the primary decider.
"""

import re

# Phrases that signal the request is not a single mechanical step.
ESCALATE = re.compile(
    r"\b(?:"
    r"and\s+then|after\s+that|afterwards|"
    r"figure\s+out|work\s+out|find\s+out\s+how|"
    r"investigate|research\s+(?:this|that|into)|do\s+research|deep\s+dive|"
    r"keep\s+(?:trying|going|working)|until\s+(?:it|you)\s+|"
    r"make\s+it\s+work|get\s+it\s+working|"
    r"until\s+(?:it\s+works|tests?\s+pass|green)|"
    r"clean\s+up|improve\s+optimi[sz]e|refactor\s+(?:the\s+)?(?:whole|entire)|"
    r"from\s+scratch|build\s+(?:me\s+)?a\s+\w+\s+(?:app|api|server|service)\b|"
    r"set\s+(?:up|up)\s+|"
    # A considered deliverable means synthesis, not a single lookup.
    r"write\s+(?:me\s+)?(?:up\s+)?(?:a\s+|an\s+)?(?:report|recommendation|summary|"
    r"analysis|proposal|document|plan|write[- ]?up)|"
    r"recommendation|pros\s+and\s+cons|trade[- ]?offs?|"
    r"compare\s+\w+\s+(?:and|vs\.?|versus)"
    r")",
    re.I,
)

# An explicit request to use the other agent.
EXPLICIT_HERMES = re.compile(r"\b(?:use|ask|delegate\s+to|hand\s+(?:this\s+)?(?:off|it)\s+to)\s+hermes\b", re.I)

# Tools a single call fully satisfies. Asking for one of these and nothing else
# is the analyzer's own statement that the job is one step.
SINGLE_SHOT_TOOLS = frozenset(
    {"bash", "pc_control", "web_search", "fetch_url", "describe_image"}
)


def needs_hermes(message):
    """True when the request shows signs of needing planning or autonomy."""
    text = message or ""

    if EXPLICIT_HERMES.search(text):
        return True

    return bool(ESCALATE.search(text))


def can_demote(analysis, message):
    """True when an escalated request is really a single tool call.

    The analyzer is lenient in both directions, and because a false positive
    here routes a one-line command through a heavyweight agent, the demotion has
    to be tight. It only fires when the analyzer's own plan is a single
    single-shot tool and the wording carries no signal of planning, iteration,
    or synthesis -- if the model wanted an agent for a reason it did not put in
    the wording, this cannot see it, and it stays escalated.
    """
    if needs_hermes(message):
        return False

    tools = list(getattr(analysis, "tools", None) or [])

    if len(tools) != 1 or tools[0] not in SINGLE_SHOT_TOOLS:
        return False

    # A multi-step plan or a synthesised deliverable contradicts the demotion
    # even when it somehow named only one tool.
    if getattr(analysis, "action", "") in _DELIVERABLE_ACTIONS:
        return False

    return True


_DELIVERABLE_ACTIONS = frozenset({"plan", "research", "review", "implement", "analyze"})


def check(analysis, message, reason_prefix="model"):
    """Return ``(delegate, reason)`` for a request.

    Escalates on demand, and demotes the narrow case where the model escalated
    something it had itself scoped to a single tool call. Anything ambiguous
    stays with HERMES, because a slower correct answer beats a wrong fast one.
    """
    if analysis.delegate:
        if can_demote(analysis, message):
            return False, (
                f"{reason_prefix}: escalated, but scoped to a single "
                f"{analysis.tools[0]} call, so SAGE handles it"
            )

        return True, f"{reason_prefix}: flagged as needing an agent"

    if needs_hermes(message):
        return True, f"{reason_prefix}: wording needs planning or multiple steps"

    return False, f"{reason_prefix}: single step SAGE can do with a tool"
