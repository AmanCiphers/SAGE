from sage.core.llm import LLMClient


class Handler:
    def __init__(self):
        self.llm = LLMClient()

    def handle(self, message, conversation=None, model=None):
        messages = []

        if conversation:
            messages = conversation

        messages.append({
            "role": "user",
            "content": message
        })

        return self.llm.chat(messages, model=model)
