KNOWN_TOOLS = ("web_search", "fetch_url")


class TaskAnalysis:
    def __init__(self, intent, action, delegate, target=None, tools=None):
        self.intent = intent
        self.action = action
        self.delegate = delegate
        self.target = target
        self.tools = list(tools or [])

    @property
    def uses_web(self):
        return "web_search" in self.tools
