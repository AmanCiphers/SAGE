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
        self.analysis = None
        self.result = None
        self.error = None
        
    
    def set_type_from_analysis(self):
        action = self.analysis.action.lower()

        if "code" in action or "create" in action or "build" in action:
            self.type = JobType.CODE

        elif "browser" in action or "search" in action:
            self.type = JobType.BROWSER

        elif "terminal" in action or "command" in action or "run" in action:
            self.type = JobType.TERMINAL

        else:
            self.type = JobType.GENERAL
