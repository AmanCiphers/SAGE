class ModelRouter:
    def route(self, job):
        if job.complexity == "complex":
            return "powerful-model"

        if job.type.value == "code":
            return "coding-model"

        if job.type.value == "browser":
            return "reasoning-model"

        if job.type.value == "terminal":
            return "reasoning-model"

        return "general-model"
