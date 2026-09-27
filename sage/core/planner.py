from sage.core.job import Job


class Planner:
    def create_plan(self, job):
        print(f"Creating plan for: {job.request}")

        return [

            "execute_task",

        ]
