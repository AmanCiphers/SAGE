from sage.core.job import Job, JobStatus
from sage.core.planner import Planner
from sage.core.executor import Executor
from sage.core.verifier import Verifier
from sage.core.router import ModelRouter
from sage.core.job_analyzer import Analyzer


class Orchestrator:
    def __init__(self):
        self.planner = Planner()
        self.executor = Executor()
        self.verifier = Verifier()
        self.router = ModelRouter()
        self.analyzer = Analyzer()

    def run(self, job):
        
        self.analyzer.analyze(job)
        
        job.status = JobStatus.PLANNING
        job.plan = self.planner.create_plan(job)
        print(f"Plan created for job: {job.request}")
        
        job.model = self.router.route(job)
        print("Model selected for job:", job.model)
          
        job.status = JobStatus.RUNNING
        
        result = self.executor.execute(job)
        
        if not result.success:
            job.status = JobStatus.FAILED
            print(
                f"Job '{job.request}' failed during execution. with error: {result.error}")
            return
        print("Execution result:", result.output)

        verified = self.verifier.verify(result)
        if verified:
            job.status = JobStatus.COMPLETED
            print(f"Job '{job.request}' has completed successfully.")
        else:
            job.status = JobStatus.FAILED
            print(f"Job '{job.request}' has failed verification.")
