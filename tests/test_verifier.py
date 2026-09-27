"""Verification of a finished answer."""

import pytest

from sage.core.verifier import Verifier

ANSWERS = [
    "Darwin",
    "42",
    "The release notes say 1.85.0 shipped on Tuesday.",
    "I can't reach that URL directly, but the changelog lists it as fixed in 1.4.",
    "Here is the diff:\n\n```diff\n- old\n+ new\n```",
    "I could not find a matching record in the database, so I did not change anything.",
]


class TestAccepts:
    @pytest.mark.parametrize("answer", ANSWERS)
    def test_real_answers_pass(self, answer):
        ok, reason = Verifier().check(answer)
        assert ok, reason

    def test_short_answer_is_not_rejected_for_length(self):
        # A length floor would fail correct one-word replies.
        assert Verifier().check("42")[0]


class TestRejects:
    @pytest.mark.parametrize(
        "answer",
        [
            "I can't help with that.",
            "I'm sorry, but I cannot assist with this request.",
            "I am unable to do that.",
            "As an AI language model, I must decline.",
            "Unfortunately, I cannot provide that.",
        ],
    )
    def test_refusals_fail(self, answer):
        assert not Verifier().check(answer)[0]

    def test_refusal_inside_a_real_answer_passes(self):
        answer = (
            "I can't reach that URL directly, but the changelog lists it as fixed "
            "in version 1.4, so upgrading should resolve the error you saw."
        )
        assert Verifier().check(answer)[0]

    @pytest.mark.parametrize("result", [None, "", "   ", "\n\n", 42, [], {}])
    def test_empty_or_wrong_type_fails(self, result):
        ok, reason = Verifier().check(result)
        assert not ok
        assert reason

    def test_traceback_fails(self):
        answer = (
            "Here is what happened:\n"
            'Traceback (most recent call last):\n'
            '  File "sage/core/handler.py", line 88, in run\n'
            "    raise RuntimeError"
        )
        ok, reason = Verifier().check(answer)
        assert not ok
        assert "traceback" in reason

    def test_provider_error_fails(self):
        ok, reason = Verifier().check("API call failed after 3 retries: HTTP 429")
        assert not ok
        assert "provider" in reason

    def test_verify_returns_a_bool(self):
        assert Verifier().verify("a real answer") is True
        assert Verifier().verify("I can't help with that.") is False


class TestFabricatedResult:
    """A reply that claims an action no tool performed is a failed answer.

    This is worse than an honest refusal: the user walks away believing
    something happened on their machine when nothing did.
    """

    @pytest.mark.parametrize("text", [
        "Terminal opened (another new window).",
        "I've launched the app.",
        "Done - Safari is open now.",
        "Deleted the file.",
        "Reminder scheduled.",
        "Screenshot taken.",
        "Volume muted.",
        "I saved the file to disk.",
    ])
    def test_claim_without_a_tool_is_rejected(self, text):
        ok, reason = Verifier().check(text, tools_used=set())

        assert not ok
        assert "no tool ran" in reason

    @pytest.mark.parametrize("text", [
        # guidance, not a claim
        "How do I open Terminal? Applications > Utilities.",
        "You can open Terminal with Cmd+Space.",
        "To do this, open the Settings app and enable the toggle.",
        "I would suggest running the tests first.",
        "Here's a plan: I created a checklist for the migration.",
        # history, not a claim about this turn
        "The file was created yesterday by the build script.",
        "I created the checklist yesterday.",
        "The migration was completed last year.",
        # nothing to do with actions
        "2 + 2 = 4",
        "Sure - what would you like me to do?",
    ])
    def test_ordinary_answers_still_pass(self, text):
        assert Verifier().check(text, tools_used=set())[0]

    @pytest.mark.parametrize("text", [
        "Terminal opened (another new window).",
        "Deleted the file.",
    ])
    def test_a_real_tool_result_is_accepted(self, text):
        assert Verifier().check(text, tools_used={"pc_control"})[0]
        assert Verifier().check(text, tools_used={"bash"})[0]

    def test_check_is_unaffected_when_tools_are_unknown(self):
        # Callers that do not track tools keep the old behaviour rather than
        # having every answer judged against an empty set.
        assert Verifier().check("Terminal opened.")[0]
