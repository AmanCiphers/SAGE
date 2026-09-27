import os

from dotenv import load_dotenv
from openai import OpenAI

from sage.core.personality import SYSTEM_PROMPT

load_dotenv()

BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


class LLMClient:
    def __init__(self, model=None):
        self.client = OpenAI(
            base_url=BASE_URL,
            api_key=os.environ.get("NVIDIA_API_KEY"),
        )

        self.model = model or DEFAULT_MODEL

    @staticmethod
    def _with_system(messages, system):
        return [
            *([{"role": "system", "content": system}] if system else []),
            *messages,
        ]

    def chat(self, messages, model=None, system=SYSTEM_PROMPT):
        """Single-shot completion.

        Pass ``system=None`` to drop the SAGE persona, e.g. for internal
        classification calls that must not answer the user directly.
        """
        response = self.client.chat.completions.create(
            model=model or self.model,
            messages=self._with_system(messages, system),
        )

        return response.choices[0].message.content

    def stream(self, messages, model=None, system=SYSTEM_PROMPT):
        """Yield content deltas as they arrive."""
        response = self.client.chat.completions.create(
            model=model or self.model,
            messages=self._with_system(messages, system),
            stream=True,
        )

        for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta.content

            if delta:
                yield delta

    def complete(self, messages, model=None, system=SYSTEM_PROMPT, tools=None):
        """Return the full assistant message, including any tool_calls.

        Used by the tool loop, which needs the call arguments, not prose.
        """
        response = self.client.chat.completions.create(
            **self._request(messages, model, system, tools),
        )

        return self._as_message(response.choices[0].message)

    def stream_events(self, messages, model=None, system=SYSTEM_PROMPT, tools=None):
        """Stream a reply that may call tools.

        Yields ``{"type": "text", "delta": str}`` per content delta and
        ``{"type": "tool_call", "name", "arguments"}`` as each call completes,
        finishing with ``{"type": "done", "message": {...}}``.

        Tool-call arguments arrive fragmented across chunks, so they are
        accumulated by index and only emitted once the stream ends.
        """
        response = self.client.chat.completions.create(
            **self._request(messages, model, system, tools),
            stream=True,
        )

        content = []
        calls = {}
        order = []

        for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta.content:
                content.append(delta.content)
                yield {"type": "text", "delta": delta.content}

            for call in delta.tool_calls or []:
                index = call.index if call.index is not None else 0

                if index not in calls:
                    calls[index] = {"id": "", "name": "", "arguments": ""}
                    order.append(index)

                if call.id:
                    calls[index]["id"] = call.id

                if call.function:
                    if call.function.name:
                        calls[index]["name"] += call.function.name
                    if call.function.arguments:
                        calls[index]["arguments"] += call.function.arguments

        tool_calls = [
            {
                "id": calls[index]["id"],
                "type": "function",
                "function": {
                    "name": calls[index]["name"],
                    "arguments": calls[index]["arguments"],
                },
            }
            for index in order
        ]

        message = {"role": "assistant", "content": "".join(content) or None}

        if tool_calls:
            message["tool_calls"] = tool_calls

        for call in tool_calls:
            yield {
                "type": "tool_call",
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
            }

        yield {"type": "done", "message": message}

    def _request(self, messages, model, system, tools):
        request = {
            "model": model or self.model,
            "messages": self._with_system(messages, system),
        }

        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        return request

    @staticmethod
    def _as_message(message):
        payload = {"role": "assistant", "content": message.content}

        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]

        return payload
