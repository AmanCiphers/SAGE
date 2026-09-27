import re

from sage.core.llm import LLMClient
from sage.tools import registry
from sage.tools.loop import run_tool_loop
from sage.tools.search import search
from sage.tools.web import fetch_url

RESEARCH_SYSTEM = (
    "Web results retrieved for this request. Rely on them, and say plainly "
    "if they do not answer the question."
)

# Injected when retrieval was asked for but could not happen. Without this the
# model silently answers from memory -- or worse, restates its own earlier
# answer from this conversation as though it were freshly verified.
RETRIEVAL_FAILED_SYSTEM = (
    "Live web retrieval FAILED for this request, so you have no current "
    "information. You may still answer from general knowledge, but you MUST "
    "state clearly at the start that you could not verify it online. Do not "
    "present anything from earlier turns in this conversation as if it were "
    "freshly checked, and do not invent specifics like prices, names, or "
    "contact details you cannot support."
)

DOMAIN = re.compile(
    r"\b(?:https?://)?(?:www\.)?"
    r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"
    r"(?::\d+)?(?:/[^\s]*)?",
    re.I,
)

# Bare filenames that look like domains but are not sites worth fetching.
NOT_DOMAINS = {
    "js", "json", "py", "md", "css", "html", "ts", "tsx", "jsx", "txt", "sh",
    "yml", "yaml", "toml", "lock", "log", "csv", "xml", "sql", "db", "png",
    "jpg", "jpeg", "svg", "pdf", "zip", "env", "gitignore",
}


class Handler:
    def __init__(self, llm=None, task_store=None, task_manager=None):
        self.llm = llm or LLMClient()
        self.task_store = task_store
        self.task_manager = task_manager

    def handle(self, message, conversation=None, model=None, research=None,
               retrieval_error=None, surface="cli"):
        content = ""

        for event in self.run(message, conversation, model, research,
                              retrieval_error, surface):
            if event["type"] == "text":
                content += event["delta"]
            elif event["type"] == "done":
                return event.get("content") or content

        return content

    def run(self, message, conversation=None, model=None, research=None,
            retrieval_error=None, surface="cli"):
        """Yield tool-loop events for a request handled by SAGE itself."""
        actions = registry.build_actions(
            manager=self.task_manager, store=self.task_store
        ) if self.task_manager else registry.build_actions()

        return run_tool_loop(
            self.llm,
            message,
            conversation=conversation,
            model=model,
            actions=actions,
            surface=surface,
            extra_system=self._extra_system(research, retrieval_error),
        )

    def stream(self, message, conversation=None, model=None, research=None,
               retrieval_error=None, surface="cli"):
        """Yield only the text deltas, for callers that do not want events."""
        for event in self.run(message, conversation, model, research,
                              retrieval_error, surface):
            if event["type"] == "text":
                yield event["delta"]

    @staticmethod
    def _extra_system(research, retrieval_error):
        notes = []

        if research:
            notes.append(RESEARCH_SYSTEM + "\n\n" + research)
        elif retrieval_error:
            notes.append(
                RETRIEVAL_FAILED_SYSTEM + f"\n\nRetrieval error: {retrieval_error}"
            )

        return notes

    @staticmethod
    def named_domain(message):
        """Return a bare domain if the user named one, else None."""
        for match in DOMAIN.finditer(message):
            candidate = match.group(1).lower()

            if candidate.split(".")[-1] in NOT_DOMAINS:
                continue

            if "." in candidate:
                return candidate

        return None

    def retrieve(self, message):
        """Gather current information for a request.

        Returns ``(context, error)``. Exactly one is set. A named domain is
        fetched directly, which is both faster and more reliable than
        searching for it.
        """
        domain = self.named_domain(message)

        if domain:
            context, error = self._fetch(f"https://{domain}", domain)

            if context:
                return context, None

            print(f"[TOOLS] Direct fetch of {domain} failed; falling back to search.")

        try:
            found = search(message, num_results=5, include_raw=True)
        except Exception as error:
            return None, f"web search failed: {error}"

        results = found.get("results") or []

        if not results:
            return None, "web search returned no results"

        print(f"[TOOLS] Search provider: {found.get('provider')}")

        blocks = []

        for index, result in enumerate(results, start=1):
            blocks.append(
                f"[{index}] {result['title']}\n"
                f"    {result['url']}\n"
                f"    {result['snippet']}"
            )

        top = results[0]

        if top.get("text"):
            # The provider already returned the page text, so skip the fetch.
            blocks.append(f"\nFull text of {top['url']}:\n{top['text']}")
        else:
            context, _ = self._fetch(top["url"], top["title"])

            if context:
                blocks.append(context)

        return "\n\n".join(blocks), None

    @staticmethod
    def _fetch(url, label):
        try:
            page = fetch_url(url)
        except Exception as error:
            print(f"[TOOLS] fetch_url failed for {url}: {error}")
            return None, str(error)

        text = page["text"]

        if not text:
            return None, f"no readable content at {url}"

        return f"Full text of {url} ({label}):\n{text}", None
