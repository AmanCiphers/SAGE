from sage.core.job_analyzer import Analyzer
from sage.core.decision_maker import DecisionMaker
from sage.core.handler import Handler
from sage.core.hermes import Hermes


class Orchestrator:
    def __init__(self):
        self.analyzer = Analyzer()
        self.decision_maker = DecisionMaker()
        self.handler = Handler()
        self.hermes = Hermes()

    def run(self, message):
        analysis = self.analyzer.analyze(message)

        decision = self.decision_maker.decide(
            analysis,
            message
        )

        if decision.type == "handle":
            return self.handler.handle(message)

        if decision.type == "delegate":
            return self.hermes.run(decision.task)
