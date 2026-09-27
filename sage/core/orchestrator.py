from sage.core.job_analyzer import Analyzer
from sage.core.decision_maker import DecisionMaker
from sage.core.handler import Handler
from sage.core.job import Job, JobStatus
from sage.core.hermes import Hermes
from sage.core.verifier import Verifier


class Orchestrator:
    def __init__(self):
        self.analyzer = Analyzer()
        self.decision_maker = DecisionMaker()
        self.handler = Handler()
        self.hermes = Hermes()
        self.verifier = Verifier()

    def run(self, message):
        print(f"[SAGE] Received request: {message}")

        job = Job(message)

        print(f"[JOB] Created: {job.request}")
        print(f"[JOB] Status: {job.status.value}")

        print("[ANALYZER] Analyzing request...")
        analysis = self.analyzer.analyze(message)

        print(f"[ANALYZER] Intent: {analysis.intent}")
        print(f"[ANALYZER] Action: {analysis.action}")
        print(f"[ANALYZER] Delegate: {analysis.delegate}")
        print(f"[ANALYZER] Target: {analysis.target}")

        job.analysis = analysis
        job.set_type_from_analysis()

        print(f"[JOB] Type: {job.type.value}")

        print("[DECISION] Making execution decision...")
        decision = self.decision_maker.decide(
            analysis,
            message
        )

        print(f"[DECISION] Handler: {decision.handler}")
        print(f"[DECISION] Model: {decision.model}")
        print(f"[DECISION] Task: {decision.task}")

        if decision.handler == "sage":
            print("[SAGE] Handling request directly...")

            job.status = JobStatus.RUNNING

            result = self.handler.handle(
                message,
                model=decision.model
            )

            job.result = result
            verified = self.verifier.verify(result)
            
            if verified:
                job.status = JobStatus.COMPLETED
                print("[JOB] Status: completed")
                print("[SAGE] Request completed.")
                return result
            
            job.status = JobStatus.FAILED


            job.error = "Result verification failed"

            print("[JOB] Status: failed")
            print("[SAGE] Request failed verification.")

            return None
                        
            

        if decision.handler == "hermes":
            print("[HERMES] Delegating task...")
            print(f"[HERMES] Model: {decision.model}")

            job.status = JobStatus.RUNNING

            result = self.hermes.run(
                decision.task,
                model=decision.model
            )

            job.result = result
            verified = self.verifier.verify(result)

            if verified:
                job.status = JobStatus.COMPLETED
                print("[JOB] Status: completed")
                print("[HERMES] Task completed.")
                return result

            job.status = JobStatus.FAILED
            job.error = "Result verification failed"

            print("[JOB] Status: failed")
            print("[HERMES] Task failed verification.")

            return None
