from sage.core.decision import Decision


class DecisionMaker:
    def decide(self, analysis, original_message):
        if analysis.delegate:
            return Decision(
                type="delegate",
                target=analysis.target,
                task=original_message
            )

        return Decision(
            type="handle",
            target="sage",
            task=original_message
        )
