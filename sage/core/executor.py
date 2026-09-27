class ExecutionResult:
    def __init__(self, success, output, error = None):
        self.success = success
        self.output = output
        self.error = error
        
        
class Executor:
    def execute(self, job):
        results = []
        
        for step in job.plan:
            print(f"Executing step: {step}")
            results.append(f" {step}")
            
        return ExecutionResult(True, results)