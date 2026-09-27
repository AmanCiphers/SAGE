import re
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
        self.analysis = None
        self.result = None
        self.error = None
        self.routing_reason = None

    def set_type_from_capability(self):
        """Derive the job type from what SAGE can actually do for it.

        This replaces keyword matching on the analyzer's free-text action, which
        produced a label nothing read.
        """
        message = f"{self.request} {self.analysis.action if self.analysis else ''}".lower()

        if re.search(r"\b(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|net|org|io|dev|app|co)\b", message):
            self.type = JobType.BROWSER
        elif re.search(r"\b(?:code|repo|script|test|build|compile|refactor|function|bug)\w*\b", message):
            self.type = JobType.CODE
        elif re.search(r"\b(?:run|install|command|shell|terminal|git|npm|pip)\b", message):
            self.type = JobType.TERMINAL
        else:
            self.type = JobType.GENERAL
