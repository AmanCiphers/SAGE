from sage.core.decision import Decision


class DecisionMaker:
    def decide(self, analysis, original_message):
        if analysis.delegate:
            return Decision(
                handler="hermes",
                model="nvidia/nemotron-3-ultra-550b-a55b",
                task=original_message
            )

        return Decision(
            handler="sage",
            model="nvidia/nemotron-3-ultra-550b-a55b",
            task=original_message
        )
