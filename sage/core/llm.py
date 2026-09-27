import os
from dotenv import load_dotenv
from openai import OpenAI
from sage.core.personality import SYSTEM_PROMPT

load_dotenv()


class LLMClient:
    def __init__(self):
        self.client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.environ.get("NVIDIA_API_KEY")
        )

        self.model = "nvidia/nemotron-3-ultra-550b-a55b"

    def chat(self, messages):
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            *messages
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages
        )

        return response.choices[0].message.content
