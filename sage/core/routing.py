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


def needs_hermes(message):
    """True when the request shows signs of needing planning or autonomy."""
    text = message or ""

    if EXPLICIT_HERMES.search(text):
        return True

    return bool(ESCALATE.search(text))


def check(analysis, message, reason_prefix="model"):
    """Return ``(delegate, reason)`` for a request.

    Only escalates. A model that says delegate stays delegate, and a model that
    says handle stays handle unless the wording plainly needs an agent.
    """
    if analysis.delegate:
        return True, f"{reason_prefix}: flagged as needing an agent"

    if needs_hermes(message):
        return True, f"{reason_prefix}: wording needs planning or multiple steps"

    return False, f"{reason_prefix}: single step SAGE can do with a tool"
