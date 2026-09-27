from sage.core.llm import LLMClient
from sage.tools import registry
from sage.tools.loop import run_tool_loop


class Handler:
    """Runs a request through SAGE's own tool loop.

    Retrieval is not done up front. It used to search once here and inject the
    results as system context, which cost a second round of fetches whenever the
    model then searched again, and the injected text was already stale by the
    time the answer was written. The model now calls ``web_search`` and
    ``fetch_url`` itself, so whatever backs the answer was fetched during this
    turn.
    """

    def __init__(self, llm=None, task_store=None, task_manager=None,
                 chat_store=None):
        self.llm = llm or LLMClient()
        self.task_store = task_store
        # Holds the message table. Falls back to the task store when the caller
        # has only that.
        self.chat_store = chat_store or task_store
        self.task_manager = task_manager

    def handle(self, message, conversation=None, model=None, surface="cli"):
        content = ""

        for event in self.run(message, conversation, model, surface):
            if event["type"] == "text":
                content += event["delta"]
            elif event["type"] == "done":
                return event.get("content") or content

        return content

    def run(self, message, conversation=None, model=None, surface="cli",
            approved_command=None, conversation_id=None):
        """Yield tool-loop events for a request handled by SAGE itself."""
        actions = registry.build_actions(
            manager=self.task_manager, store=self.task_store,
            conversation_id=conversation_id, chat_store=self.chat_store,
        ) if self.task_manager else registry.build_actions(
            conversation_id=conversation_id, chat_store=self.chat_store
        )

        return run_tool_loop(
            self.llm,
            message,
            conversation=conversation,
            model=model,
            actions=actions,
            surface=surface,
            approved_command=approved_command,
        )

    def stream(self, message, conversation=None, model=None, surface="cli"):
        """Yield only the text deltas, for callers that do not want events."""
        for event in self.run(message, conversation, model, surface):
            if event["type"] == "text":
                yield event["delta"]
