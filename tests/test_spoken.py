"""The spoken companion line.

This text exists to be heard, not read, so the assertions here are about what
survives into a TTS engine: no markdown syntax, no emoji, no paths spelled
character by character, and a length that stays one breath.
"""

import pytest

from sage.core.spoken import MAX_WORDS, SpokenSummarizer, to_speech


class TestToSpeech:
    @pytest.mark.parametrize("raw,expected", [
        ("**Done.** Your IP is `45.115.179.166`.", "Done. Your IP is 45.115.179.166."),
        ("- opened the terminal\n- ran the build", "opened the terminal ran the build."),
        ("Here is: all tests passed 🎉", "all tests passed."),
        ("Check https://example.com/docs for more", "Check that link for more."),
        ("It wrote /Users/aman/notes.md for you", "It wrote that file for you."),
        ("## Result\nThe build is green.", "Result The build is green."),
        ("Read the file at `sage/core/handler.py`", "Read the file at that file."),
    ])
    def test_markup_is_stripped(self, raw, expected):
        assert to_speech(raw) == expected

    def test_line_is_clamped_to_a_breath(self):
        long_answer = " ".join(f"word{i}" for i in range(200))

        line = to_speech(long_answer)

        assert line is not None
        assert len(line.split()) <= MAX_WORDS
        # Cut mid-sentence at worst, but never left dangling on a comma.
        assert not line.rstrip(".").endswith((",", ";", ":"))

    def test_recent_sentence_boundary_wins(self):
        filler = " ".join(f"w{i}" for i in range(15))
        line = to_speech(f"{filler}. That is the short version of it. {filler}.")

        assert len(f"{filler}. That is the short version of it. {filler}.".split()) > MAX_WORDS

        assert line.endswith("That is the short version of it.")

    def test_emoji_and_punctuation_only_input_yields_nothing(self):
        # None means "no spoken line", which callers handle as normal rather
        # than as a failure.
        for junk in ("", "   ", "\n\n", "🎉🎉", "...", "—"):
            assert to_speech(junk) is None

    def test_ends_as_a_sentence(self):
        line = to_speech("the terminal is open")

        assert line.endswith(".")

    def test_speech_tag_is_removed(self):
        assert to_speech("Speak: the build is green") == "the build is green."

    def test_non_string_is_ignored(self):
        assert to_speech(None) is None
        assert to_speech(42) is None


class _Stub:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def chat(self, messages, system=None):
        self.calls.append((messages, system))
        if self.error:
            raise self.error
        return self.reply


class TestSummarizer:
    def test_returns_a_speakable_line(self):
        s = SpokenSummarizer(llm=_Stub("**All set.** Terminal is open."))

        assert s.summarize("I opened Terminal with `open -a Terminal`.") == (
            "All set. Terminal is open."
        )

    def test_sees_the_question_and_the_answer(self):
        llm = _Stub("Done.")
        SpokenSummarizer(llm=llm).summarize("The build passed.", "did the build pass?")

        prompt = llm.calls[0][0][0]["content"]
        assert "did the build pass?" in prompt
        assert "The build passed." in prompt

    @pytest.mark.parametrize("answer", [None, "", "   "])
    def test_no_answer_means_no_line_and_no_call(self, answer):
        llm = _Stub("unused")
        assert SpokenSummarizer(llm=llm).summarize(answer, "q") is None
        assert llm.calls == []

    def test_provider_failure_never_raises(self):
        # The real answer is already on screen; a missing spoken line must not
        # take the turn down with it.
        s = SpokenSummarizer(llm=_Stub(error=RuntimeError("503 Resource exhausted")))

        assert s.summarize("the build passed", "did it pass?") is None

    def test_uses_the_small_analyzer_model_by_default(self):
        # Summarising a finished answer is the same class of work as routing.
        from sage.core.llm import ANALYZER_MODEL, DEFAULT_MODEL

        assert SpokenSummarizer().llm.model == ANALYZER_MODEL
        assert SpokenSummarizer().llm.model != DEFAULT_MODEL


class TestSpokenInstructions:
    """The instructions are the whole behaviour, so they are pinned.

    The model is what decides the wording, so a future edit that quietly drops
    the deferral instruction would not fail any behavioural test -- it would
    just start reading tables aloud.
    """

    def test_defers_to_the_screen_instead_of_reciting_detail(self):
        from sage.core.spoken import SYSTEM

        lowered = SYSTEM.lower()

        assert "screen" in lowered
        assert "recite" in lowered or "recap" in lowered

    def test_keeps_the_style_the_user_asked_for(self):
        from sage.core.spoken import SYSTEM

        assert "sir" in SYSTEM.lower()
        assert "butler" in SYSTEM.lower()

    def test_summarises_the_answer_that_is_on_screen(self):
        llm = _Stub("Done.")
        SpokenSummarizer(llm=llm).summarize("THE ANSWER", "THE QUESTION")

        messages, system = llm.calls[0]
        prompt = messages[0]["content"]

        assert "THE ANSWER" in prompt
        assert "THE QUESTION" in prompt
        assert "on their screen" in prompt
        # The behavioural rules travel in the system prompt, not the user turn.
        assert "screen" in system.lower()


class TestSpokenPunctuation:
    """Punctuation that looks fine on screen can be audible nonsense.

    These are the characters a TTS engine either names out loud or reads
    straight through, joining words that should be separate.
    """

    @pytest.mark.parametrize("raw,expected", [
        # A non-breaking hyphen is not a word break: engines read it or skip it.
        ("AR hand\u2011tracking demo", "AR hand-tracking demo."),
        ("en\u2013dash and em\u2014dash", "en, dash and em, dash."),
        ("curly \u2018quotes\u2019 here", "curly 'quotes' here."),
        ("a\u2026b", "a...b."),
        ("joined\u00a0together", "joined together."),
        ("non\u2011breaking\u00a0hyphen", "non-breaking hyphen."),
    ])
    def test_unicode_punctuation_is_normalised(self, raw, expected):
        assert to_speech(raw) == expected

    def test_no_typographic_punctuation_survives(self):
        line = to_speech("A\u2011B \u2014 C \u2018D\u2019 \u2013 E")

        # An ASCII hyphen is fine -- "hand-tracking" is meant to be heard as
        # two words -- so only the typographic variants are forbidden.
        assert not set(line) & set("\u2013\u2014\u2018\u2019\u201c\u201d")
        assert "hand-tracking" in to_speech("AR hand\u2011tracking demo")
