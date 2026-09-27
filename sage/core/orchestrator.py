from sage.core import routing
from sage.core.handler import Handler
from sage.core.hermes import Hermes
from sage.core.job import Job, JobStatus
from sage.core.job_analyzer import Analyzer
from sage.core.decision_maker import DecisionMaker
from sage.core.verifier import Verifier


def _summarize(result, limit=220):
    """One short line about a tool result, for the client."""
    if isinstance(result, str):
        return result[:limit]

    if isinstance(result, dict):
        if "error" in result:
            return str(result["error"])[:limit]

        for key in ("note", "output", "analysis", "results", "tasks", "status"):
            if key in result:
                value = result[key]

                if isinstance(value, (list, dict)):
                    return f"{key}: {len(value)} item(s)"

                if key == "status" and isinstance(value, int):
                    return f"HTTP {value}"

                return str(value)[:limit]

        if "text" in result:
            return f"{len(result['text'])} chars of text"

        return ", ".join(sorted(result))[:limit]

    return str(result)[:limit]


class Orchestrator:
    """Analyze a request, route it, run it, verify the result.

    ``run_stream`` is the single implementation. ``run`` drains it and hands
    back the finished :class:`Job`.
    """

    def __init__(self, analyzer=None, decision_maker=None, handler=None, hermes=None,
                 verifier=None, surface="cli", local_tools=True):
        self.surface = surface
        # The analyzer must know which tools the surface actually exposes,
        # or it will route bash work to SAGE on a surface that refuses it.
        self.analyzer = analyzer or Analyzer(surface=surface, local_tools=local_tools)
        self.decision_maker = decision_maker or DecisionMaker()
        self.handler = handler or Handler()
        self.hermes = hermes or Hermes()
        self.verifier = verifier or Verifier()

    def run(self, message, conversation=None, surface="cli"):
        """Execute a request end to end and return the finished Job."""
        for event in self.run_stream(message, conversation, surface=surface):
            if "job" in event:
                return event["job"]

        return None

    def run_stream(self, message, conversation=None, surface="cli"):
        """Yield progress events, then ``delta`` events, then a final ``done``.

        Event keys: ``stage``, ``handler``, ``type``, ``delta``, ``error``,
        ``done``, ``response``, ``status``, ``job``.
        """
        print(f"[SAGE] Received request: {message}")
        yield {"stage": "analyzing"}

        job = Job(message)
        print(f"[JOB] Created: {job.request}")
        print(f"[JOB] Status: {job.status.value}")
        print("[ANALYZER] Analyzing request...")

        try:
            job.analysis = self.analyzer.analyze(message)
        except Exception as error:
            return self._abort(job, error)

        print(f"[ANALYZER] Intent: {job.analysis.intent}")
        print(f"[ANALYZER] Action: {job.analysis.action}")
        print(f"[ANALYZER] Delegate: {job.analysis.delegate}")
        print(f"[ANALYZER] Target: {job.analysis.target}")
        print(f"[ANALYZER] Tools: {job.analysis.tools or 'none'}")
        print(f"[JOB] Type: {job.type.value}")

        print("[DECISION] Making execution decision...")
        delegate, reason = routing.check(job.analysis, message)
        job.routing_reason = reason
        job.set_type_from_capability()
        decision = self.decision_maker.decide(job.analysis, message, delegate=delegate)
        job.model = decision.model
        print(f"[DECISION] Reason: {reason}")

        print(f"[DECISION] Handler: {decision.handler}")
        print(f"[DECISION] Model: {decision.model}")
        print(f"[DECISION] Task: {decision.task}")

        yield {
            "stage": "routing",
            "handler": decision.handler,
            "type": job.type.value,
            "reason": reason,
        }

        job.status = JobStatus.RUNNING
        chunks = []

        try:
            if decision.handler == "hermes":
                print("[HERMES] Delegating task...")
                yield {"stage": "delegating"}

                # Subprocess output only lands all at once, so emit it as one delta.
                output = self.hermes.run(decision.task, model=decision.model)
                chunks.append(output)

                if output:
                    yield {"delta": output}
            else:
                print("[SAGE] Handling request directly...")

                research = None
                retrieval_error = None

                if job.analysis.uses_web:
                    print(f"[TOOLS] Enabled: {', '.join(job.analysis.tools)}")
                    yield {"stage": "retrieving", "tools": job.analysis.tools}

                    research, retrieval_error = self.handler.retrieve(job.request)

                    if retrieval_error:
                        # Never let a silent fallback read as a researched
                        # answer: the UI is told, and so is the model.
                        print(f"[TOOLS] Retrieval failed: {retrieval_error}")
                        yield {
                            "notice": f"web retrieval failed - {retrieval_error}. answer is unverified."
                        }
                    else:
                        print("[TOOLS] Retrieval succeeded.")

                for event in self.handler.run(
                    job.request,
                    conversation=conversation,
                    model=decision.model,
                    research=research,
                    retrieval_error=retrieval_error,
                    surface=surface,
                ):
                    if event["type"] == "text":
                        chunks.append(event["delta"])
                        yield {"delta": event["delta"]}

                    elif event["type"] == "tool_pending":
                        print(f"[TOOL] calling {event['name']}")
                        yield {
                            "stage": "tool",
                            "name": event["name"],
                            "arguments": event["arguments"],
                        }

                    elif event["type"] == "tool":
                        result = event["result"]
                        failed = isinstance(result, dict) and "error" in result
                        print(f"[TOOL] {event['name']} -> {'error' if failed else 'ok'}")
                        yield {
                            "tool": event["name"],
                            "ok": not failed,
                            "summary": _summarize(result),
                        }

                    elif event["type"] == "done":
                        loop_error = event.get("error")

                        if loop_error:
                            print(f"[TOOL] loop ended: {loop_error}")
                            yield {"notice": loop_error}
        except Exception as error:
            return self._abort(job, error)

        job.result = "".join(chunks)

        if not self.verifier.verify(job.result):
            job.status = JobStatus.FAILED
            job.error = "Result verification failed"
            print("[JOB] Status: failed")
            yield self._done(job)
            return

        job.status = JobStatus.COMPLETED
        print("[JOB] Status: completed")
        yield self._done(job)

    def _done(self, job):
        return {
            "done": True,
            "response": job.result or "",
            "status": job.status.value,
            "job": job,
        }

    def _abort(self, job, error):
        job.status = JobStatus.FAILED
        job.error = f"{type(error).__name__}: {error}"
        print(f"[JOB] Status: failed -- {job.error}")

        yield {"error": job.error}
        yield self._done(job)
