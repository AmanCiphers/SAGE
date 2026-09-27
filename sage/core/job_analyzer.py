import json

from sage.core.analysis import TaskAnalysis
from sage.core.llm import LLMClient


class Analyzer:
    def __init__(self):
        self.llm = LLMClient()

    def analyze(self, message):
        prompt = f"""
Analyze the user's request.

Return ONLY valid JSON with these fields:

{{
    "intent": "...",
    "action": "...",
    "delegate": true or false,
    "target": "hermes" or null
}}

User request:
{message}
"""

        response = self.llm.chat([
            {
                "role": "user",
                "content": prompt
            }
        ])

        data = json.loads(response)

        return TaskAnalysis(
            intent=data["intent"],
            action=data["action"],
            delegate=data["delegate"],
            target=data["target"]
        )
