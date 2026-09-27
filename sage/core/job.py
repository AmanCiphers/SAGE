from enum import Enum


class JobType(Enum):
    GENERAL = "general"
    CODE = "code"
    BROWSER = "browser"
    TERMINAL = "terminal"


class JobStatus(Enum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job:
    def __init__(self, request, job_type):
        self.request = request
        self.type = job_type
        self.status = JobStatus.CREATED
        self.plan = []
        self.model = None
        self.complexity = None
