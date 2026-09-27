from sage.core.job import Job, JobType
from sage.core.orchestrator import Orchestrator


job = Job("Open my browser", JobType.BROWSER)

orchestrator = Orchestrator()
orchestrator.run(job)

print(job.status)
