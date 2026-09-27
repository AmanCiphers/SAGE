class Analyzer:
    def analyze(self, job):
        if "explain" in job.request.lower():
            job.complexity = "simple"
        else:
            job.complexity = "complex"

        print("Job complexity:", job.complexity)
