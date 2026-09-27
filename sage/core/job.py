from enum import Enum


class JobType(Enum):
    GENERAL = "general"
    CODE = "code"
    BROWSER = "browser"
    TERMINAL = "terminal"


class JobStatus(Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job:
    def __init__(self, request, job_type=JobType.GENERAL):
        self.request = request
        self.type = job_type
        self.status = JobStatus.CREATED
        self.model = None
        self.complexity = None
        self.result = None
        self.error = None
