from sage.core.decision import Decision
from sage.core.llm import DEFAULT_MODEL


class DecisionMaker:
    """Route an analyzed request to either SAGE itself or the Hermes agent."""

    def decide(self, analysis, original_message, delegate=None):
        """Pick a handler.

        ``delegate`` is the capability check's verdict. It is honoured when
        given, so the deterministic check can override the analyzer's guess;
        when omitted, the analyzer's own ``delegate`` flag decides.
        """
        if delegate is None:
            delegate = analysis.delegate

        return Decision(
            handler="hermes" if delegate else "sage",
            model=DEFAULT_MODEL,
            task=original_message,
        )
