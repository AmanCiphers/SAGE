class TaskAnalysis:
    def __init__(self, intent, action, delegate, target=None):
        self.intent = intent
        self.action = action
        self.delegate = delegate
        self.target = target
