"""Post-run checks on a completed answer.

The previous check only asked "is this a non-empty string", which passed
refusals, unhandled tracebacks, and provider error payloads straight through as
successful answers. These are the shapes that actually indicate a failed run.

Deliberately *not* checked: minimum length. "42" and "Darwin" are valid answers,
and a length floor would reject correct short replies.
"""

import re

# A reply that is nothing but a refusal. Anchored with a length cap so a
# sentence like "I can't reach that URL, but the docs say..." still passes.
REFUSAL = re.compile(
    r"^\W*(?:"
    r"i\s*(?:can\s*not|cannot|can't|won't|will\s+not)\s+(?:help|assist|do|comply|"
    r"provide|support|fulfill|fulfil)"
    r"|i'?m\s+(?:sorry|afraid|unable)"
    r"|i\s*(?:am|'m)\s+(?:sorry|unable)"
    r"|as\s+an\s+ai(?:\s+(?:language\s+)?model)?[,.]?\s+i"
    r"|(?:that|this)\s+(?:request|is\s+something)\s+"
    r"|unfortunately[, ]\s+i\s+(?:can|could)"
    r")",
    re.I,
)

# A refusal on its own is a failed answer. A refusal inside a real answer is not.
REFUSAL_ONLY_MAX_CHARS = 200

# Output that means the run broke rather than answered.
TRACEBACK = re.compile(
    r"(?:Traceback\s*\(most\s+recent\s+call\s+last\)|"
    r"^\s*File \"[^\"]+\",\s+line\s+\d+)",
    re.M,
)

PROVIDER_ERROR = re.compile(
    r"(?:"
    r"API\s+call\s+failed\s+after\s+\d+\s+retr"
    r"|\bHTTP\s*(?:4\d\d|5\d\d)\s*:"
    r"|internal\s+server\s+error"
    r"|rate\s*limit\s*(?:exceeded|error)"
    r"|service\s+unavailable"
    r")",
    re.I,
)

# "Terminal opened (another new window)." with no tool call behind it is a
# fabricated result, and it is worse than an honest refusal: the user walks away
# believing something happened. Past tense only, so advice ("open Terminal from
# Applications") and questions are not caught.
ACTION_VERBS = (
    r"opened|launched|started|created|deleted|removed|installed|downloaded|"
    r"scheduled|sent|saved|toggled|muted|unmuted|took|taken|captured|typed|"
    r"written|done|completed"
)

CLAIMED_ACTION = re.compile(
    rf"\b(?:i|we)\b[^.]{{0,40}}?\b(?:{ACTION_VERBS})\b|\b(?:{ACTION_VERBS})\b[^.]{{0,40}}?\b(?:successfully|now|for you)\b",
    re.I,
)

# "Done - Safari is open now." is the same fabrication in a different tense.
STATE_CLAIM = re.compile(
    r"\b(?:is|are)\s+(?:now\s+)?(?:open|closed|running|installed|muted|unmuted|"
    r"playing|paused|deleted|created)\b",
    re.I,
)

# A short answer built around the verb, e.g. "Terminal opened." -- subject
# first, no first-person pronoun to key off. The lookbehind skips the passive,
# so "the file was created yesterday" is history, not a claim about this turn.
TERSE_ACTION = re.compile(
    r"^(?![^.]*\b(?:was|were|been)\b)[^.]{0,60}?\b(?:"
    + ACTION_VERBS
    + r")\b",
    re.I,
)

# A reply that hands the work back to the user. Only a failure when no tool ran,
# because quoting a command after a real result is legitimate; a model that ran
# nothing and then said "you'll need to run this yourself" just refused.
DEFERRED_TO_USER = re.compile(
    r"(?:"
    r"i\s*(?:do\s+not|don'?t)\s+have\s+access"
    r"|i\s*(?:can\s*not|cannot|can'?t|am\s+unable|'m\s+unable)\s+"
    r"(?:do|access|get|run|read|check|see|open|reach|find|look)"
    r"|you(?:'ll|\s+will)\s+need\s+to\s+\w+"
    r"|you\s+(?:can|should|will)\s+run\s+(?:this|that|the)"
    r"|run\s+this\s+in\s+(?:your\s+)?terminal"
    r"|\bdo\s+(?:this|that|it)\s+yourself\b"
    r")",
    re.I,
)

# Reasons that mean "the model described instead of doing". The orchestrator
# retries once on these instead of failing the turn.
RETRYABLE_PREFIX = "no action taken: "

TERSE_CLAIM_MAX_CHARS = 120

# Something that dates the action to the past, or hands it to someone else.
PAST_ATTRIBUTION = re.compile(
    r"\b(?:yesterday|earlier|previously|formerly|last\s+(?:week|month|year|night|time|"
    r"spring|summer|autumn|fall)|in\s+\d{4}|used\s+to)\b"
    r"|\b(?:was|were|been)\s+(?:already\s+)?\w+ed\b",
    re.I,
)

# Text that is plainly guidance rather than a claim of having done something.
GUIDANCE = re.compile(
    r"\b(plan|here'?s how|here is how|you can|you should|to do this|would|"
    r"could|steps?|instructions?|try )\b",
    re.I,
)


class Verifier:
    def check(self, result, tools_used=None):
        """Return ``(ok, reason)``.

        ``tools_used`` is the set of tool names that actually ran. When it is
        supplied and empty, a reply that claims to have performed an action is
        treated as a failure rather than an answer.
        """
        if result is None:
            return False, "no result"

        if not isinstance(result, str):
            return False, f"unexpected result type {type(result).__name__}"

        text = result.strip()

        if not text:
            return False, "empty result"

        if TRACEBACK.search(result):
            return False, "output contains a Python traceback"

        if PROVIDER_ERROR.search(result):
            return False, "output contains a provider error"

        if tools_used is not None and not tools_used:
            # Ahead of the plain refusal check, and ahead of the guidance
            # exemption below: "I can't read that for you, you should run cat"
            # is a refusal wearing advice as a disguise, and guidance words must
            # not launder it. Running nothing and declining is retryable,
            # because the model may simply not have tried.
            if DEFERRED_TO_USER.search(text):
                return False, (
                    f"{RETRYABLE_PREFIX}hands the work back to the user "
                    "without calling a tool"
                )

            if REFUSAL.match(text):
                return False, f"{RETRYABLE_PREFIX}refused without calling a tool"

        if len(text) <= REFUSAL_ONLY_MAX_CHARS and REFUSAL.match(text):
            return False, "result is a refusal, not an answer"

        if tools_used is not None and not tools_used and not GUIDANCE.search(text):
            claimed = None

            if not PAST_ATTRIBUTION.search(text):
                claimed = (
                    CLAIMED_ACTION.search(text)
                    or STATE_CLAIM.search(text)
                    or (
                        len(text) <= TERSE_CLAIM_MAX_CHARS
                        and TERSE_ACTION.match(text)
                    )
                )

            if claimed:
                return False, (
                    f"{RETRYABLE_PREFIX}claims '{claimed.group(0).strip()}' "
                    "but no tool ran to do it"
                )

        return True, "ok"

    def verify(self, result, tools_used=None):
        ok, reason = self.check(result, tools_used=tools_used)

        if not ok:
            print(f"[VERIFIER] rejected: {reason}")

        return ok
