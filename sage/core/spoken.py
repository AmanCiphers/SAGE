"""The short, spoken-aloud companion to a full answer.

The console shows two things per turn: the real response, then a one- or
two-sentence conversational line. That second one is what a voice mode reads
out, so it is treated as speech rather than as text to display.

That distinction is the whole point of the module. Markdown, emoji, bullet
characters, file paths and URLs are all readable on screen and all unusable
spoken aloud -- a TTS engine will say "asterisk asterisk bold asterisk
asterisk", or spell a path out one character at a time. So the rules are
enforced in code after generation, not just asked for in the prompt: a model
that ignores the instruction still gets plain prose out the other side.

Everything here is best-effort. A missing spoken line is a cosmetic gap, so
no failure in this module is allowed to affect the real answer.
"""

import re

from sage.core.llm import ANALYZER_MODEL, LLMClient

# A voice turn should be one breath. 32 words is roughly a long sentence and
# reads as a reply rather than as a second transcript.
MAX_WORDS = 32

SYSTEM = (
    "You write what an assistant says out loud. A calm, discreet butler "
    "addressing the user, who calls them sir.\n\n"
    "You are given the answer that is already on the user's screen. Reduce it "
    "to one or two short sentences of plain spoken English. Report the outcome, "
    "not the method. Never read out the screen: no markdown, no emoji, no "
    "bullet points, no code, no file paths, no URLs, and no lists of figures.\n\n"
    "If the answer holds more than a sentence or two of detail -- a table, a "
    "list, a build log, a stack trace, several results -- do not attempt to "
    "recite it. Say briefly that it is done and that the details are on "
    "screen, in the spirit of \"that is on your screen, sir\". Numbers that are "
    "the entire point of the answer, such as a single IP address or a yes/no "
    "verdict, are worth speaking.\n\n"
    "No preamble like \"Here is\". Present tense. Under 30 words."
)

_FENCE = re.compile(r"```.*?```|```", re.S)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_NOISE = re.compile(r"[*_`#>~]+")
_URL = re.compile(r"https?://\S+|www\.\S+")
# Absolute, multi-segment, or segment+extension. Deliberately not "contains a
# slash": that would swallow IPs and bare domains, and one leading segment.
_PATHISH = re.compile(
    r"(?<![\w.\-])"
    r"(?:"
    r"/[\w.\-]+(?:/[\w.\-]+)*"
    r"|[\w.\-]+(?:/[\w.\-]+){2,}"
    r"|[\w.\-]+/[\w.\-]+\.[A-Za-z][\w]{0,5}"
    r")"
)
_WS = re.compile(r"\s+")
# Punctuation TTS handles poorly mid-sentence: it introduces a pause and
# sounds like a list being read out.
_LIST_MARK = re.compile(r"(?:^|\s)[-*+•·]\s*")
# Invisible on screen, audible through a TTS engine: a non-breaking hyphen is
# not a word break, and most engines either read the character name or swallow
# it. Dashes become commas because an engine will happily narrate "em dash".
_UNICODE_PUNCT = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": ", ", "\u2014": ", ",
    "\u2212": "-", "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u200b": "",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00b7": ", ",
}
_TRANSLATE = {ord(k): v for k, v in _UNICODE_PUNCT.items()}
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U0000fe0f"
    "\U00002190-\U000021ff"
    "\U00002b00-\U00002bff"
    "]"
)
_SPOKEN_TAG = re.compile(
    r"^(?:say|speak|speech|spoken|voice|response|spoken_line)\s*:\s*", re.I
)
# "Here is:" is exactly what the prompt forbids, and it is dead air when read
# aloud, so it goes even when the model ignores the instruction.
_PREAMBLE = re.compile(
    r"^(?:here(?:'s| is| are)\b|okay|ok|alright|sure|certainly|of course|got it)"
    r"\b[\s,:-]*",
    re.I,
)
_ALNUM = re.compile(r"[A-Za-z0-9]")
# Removing markup leaves gaps in front of punctuation: "1.1.1.1 ." must not
# be read with a pause before the stop.
_GAP_BEFORE_STOP = re.compile(r"\s+([.,;:!?])")


def to_speech(text, max_words=MAX_WORDS):
    """Reduce generated text to something worth hearing.

    Returns None when there is nothing speakable left, so callers can treat
    an absent line as normal rather than as a failure.
    """
    if not text or not isinstance(text, str):
        return None

    line = text.translate(_TRANSLATE)
    line = _FENCE.sub(" ", line)
    line = _MD_LINK.sub(r"\1", line)
    line = _EMOJI.sub("", line)
    line = _MD_NOISE.sub(" ", line)
    line = _LIST_MARK.sub(" ", line)
    line = _SPOKEN_TAG.sub("", line)
    line = _PREAMBLE.sub("", line)
    line = _URL.sub("that link", line)
    line = _PATHISH.sub("that file", line)
    line = _WS.sub(" ", line)
    line = _GAP_BEFORE_STOP.sub(r"\1", line)
    line = _WS.sub(" ", line).strip(" -:,;")

    # Punctuation-only output ("...", "--") has no sound, and reading it aloud
    # as a line is worse than sending nothing.
    if len(line) < 2 or not _ALNUM.search(line):
        return None

    words = line.split()

    if len(words) > max_words:
        # Cut on a sentence boundary if there is a recent one, so the line
        # ends as a thought rather than mid-clause.
        head = " ".join(words[:max_words])
        cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))

        if cut >= max_words // 2:
            line = head[: cut + 1]
        else:
            line = head.rsplit(" ", 1)[0].rstrip(" ,;:-")

    if line and line[-1] not in ".!?":
        line = line.rstrip(" ,;:") + "."

    return line or None


class SpokenSummarizer:
    """Condense a finished answer into the line that gets spoken."""

    def __init__(self, llm=None, model=None, max_words=MAX_WORDS):
        self.llm = llm or LLMClient(model=model or ANALYZER_MODEL)
        self.max_words = max_words

    def summarize(self, answer, question=""):
        """Return a short spoken line, or None if one cannot be made.

        Never raises: the real answer is already on screen, so a failure here
        is a missing second line, not a broken turn.
        """
        if not answer or not str(answer).strip():
            return None

        # The screen already holds the full answer, so this is a reduction of
        # that answer and nothing else. Telling the model which one it is
        # keeps it from reaching for a fresh investigation.
        prompt = (
            "Say this out loud to the user in one or two short sentences. It is "
            "already on their screen, so summarise it rather than repeating it. "
            "If it is too much to speak, point them at the screen instead.\n\n"
            f"Question:\n{str(question).strip() or '(none given)'}"
            f"\n\nAnswer on screen:\n{str(answer).strip()}\n"
        )

        try:
            raw = self.llm.chat(
                [{"role": "user", "content": prompt}],
                system=SYSTEM,
            )
        except Exception as error:
            print(f"[SPOKEN] unavailable: {type(error).__name__}")
            return None

        try:
            return to_speech(raw, self.max_words)
        except Exception as error:
            print(f"[SPOKEN] could not format: {type(error).__name__}")
            return None
