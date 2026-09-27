import time

from sage.core import routing
from sage.core.handler import Handler
from sage.core.hermes import Hermes, ApprovalRequired
from sage.core.job import Job, JobStatus
from sage.core.job_analyzer import Analyzer
from sage.core.decision_maker import DecisionMaker
from sage.core.verifier import RETRYABLE_PREFIX, Verifier
from sage.core.approvals import PendingApproval, registry as approval_registry


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
                 verifier=None, surface="cli", local_tools=True, store=None,
                 conversation_id=None):
        self.surface = surface
        # Whether this surface may execute anything on the machine. Gating the
        # tool table is not enough on its own: HERMES has its own shell, so a
        # surface that forbids local execution must not delegate either.
        self.local_tools = local_tools
        # Used to pin a HERMES session per conversation and to learn from how
        # past routes turned out. Optional: without it HERMES runs statelessly.
        self.store = store
        self.conversation_id = conversation_id
        # The analyzer must know which tools the surface actually exposes,
        # or it will route bash work to SAGE on a surface that refuses it. It
        # also reads past route outcomes, so it needs the store too.
        self.analyzer = analyzer or Analyzer(
            surface=surface,
            local_tools=local_tools,
            store=store,
            conversation_id=conversation_id,
        )
        self.decision_maker = decision_maker or DecisionMaker()
        self.handler = handler or Handler()
        self.hermes = hermes or Hermes()
        self.verifier = verifier or Verifier()

    def run(self, message, conversation=None, surface="cli", yolo=False,
            approved_command=None):
        """Execute a request end to end and return the finished Job."""
        for event in self.run_stream(message, conversation, surface=surface,
                                     yolo=yolo, approved_command=approved_command):
            if "job" in event:
                return event["job"]

        return None

    def run_stream(self, message, conversation=None, surface="cli", yolo=False,
                   approved_command=None):
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
            yield from self._abort(job, error)
            return

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

        # HERMES runs commands on this machine too. On a surface that is not
        # allowed to execute anything, delegating would hand out exactly the
        # access the tool gate just refused, so the turn stays with SAGE.
        if decision.handler == "hermes" and not self._may_execute(surface):
            print("[DECISION] HERMES withheld on a no-execution surface")
            decision.handler = "sage"

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
        # What actually ran, so the verifier can tell a real result from an
        # invented one.
        tools_used = set()
        started = time.time()

        try:
            if decision.handler == "hermes":
                print("[HERMES] Delegating task...")
                yield {"stage": "delegating"}

                # Subprocess output only lands all at once, so emit it as one delta.
                outcome = self._hermes_turn(job, decision, yolo=yolo)

                if outcome.get("approval"):
                    job.status = JobStatus.NEEDS_APPROVAL
                    job.error = outcome.get("error") or "HERMES asked for approval"
                    job.approval = outcome["approval"]
                    print(f"[HERMES] approval required: {job.error}")
                    yield outcome["approval"].as_event()
                    yield self._done(job)
                    return

                if outcome.get("error"):
                    raise RuntimeError(outcome["error"])

                output = outcome.get("output") or ""
                chunks.append(output)

                if output:
                    yield {"delta": output}

            else:
                # One self-correction attempt: a model that narrates an action
                # instead of taking it is a recoverable mistake, and a second
                # pass with the gap named is far cheaper than failing the turn.
                attempt_request = job.request

                for attempt in (1, 2):
                    print("[SAGE] Handling request directly...")

                    # No pre-retrieval step. It used to fetch once up front and hand
                    # the results to the tool loop, which then searched again if it
                    # wanted to: two calls, double the cost, and the injected
                    # context was already stale by the time the model answered.
                    # The tool loop searches when it actually needs to, so its
                    # results are the ones that back the answer.
                    if job.analysis.uses_web:
                        print("[TOOLS] web available to the model in-loop")
                        yield {"stage": "retrieving", "tools": job.analysis.tools}

                    try:
                        for event in self.handler.run(
                            attempt_request,
                            conversation=conversation,
                            model=decision.model,
                            surface=surface,
                            approved_command=approved_command,
                            conversation_id=self.conversation_id,
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
                                tools_used.add(event["name"])
                                failed = isinstance(result, dict) and "error" in result
                                print(f"[TOOL] {event['name']} -> {'error' if failed else 'ok'}")
                                yield {
                                    "tool": event["name"],
                                    "ok": not failed,
                                    "summary": _summarize(result),
                                }

                            elif event["type"] == "approval_required":
                                # The model asked for something destructive and the tool
                                # refused. Park the turn and let the user decide; nothing
                                # destructive has run at this point.
                                pending = PendingApproval(
                                    task=job.request,
                                    model=decision.model,
                                    approved_command=event.get("command") or "",
                                    conversation_id=self.conversation_id,
                                    target=event.get("command") or "",
                                    description=(
                                        f"{event.get('reason') or 'destructive command'}: "
                                        f"{event.get('command') or ''}".strip()
                                    ),
                                )

                                job.status = JobStatus.NEEDS_APPROVAL
                                job.error = f"needs approval: {event.get('reason') or 'destructive command'}"
                                job.approval = approval_registry.add(pending)
                                print(f"[TOOL] approval required: {event.get('command')}")
                                yield job.approval.as_event()
                                yield self._done(job)
                                return

                            elif event["type"] == "done":
                                loop_error = event.get("error")

                                if loop_error:
                                    print(f"[TOOL] loop ended: {loop_error}")
                                    yield {"notice": loop_error}
                    except Exception as error:
                        yield from self._abort(job, error)
                        return

                    job.result = "".join(chunks)
                    ok, why = self.verifier.check(job.result, tools_used=tools_used)

                    if ok or attempt == 2 or RETRYABLE_PREFIX not in why:
                        verified, reason = ok, why
                        break

                    print(f"[VERIFY] {why}; retrying with a correction")
                    # The handler is given the user's own request, not the routed
                    # task description, so the correction has to be built there.
                    attempt_request = (
                        f"{job.request}\n\nYour previous answer described doing "
                        f"something instead of doing it, so nothing happened. Use a "
                        f"tool to actually do the work now, or say plainly that you "
                        f"did not do it."
                    )
                    chunks, tools_used = [], set()

            if decision.handler == "hermes":
                # HERMES runs its own tools internally and we only see its final
                # text, so there is nothing to compare a claimed action against.
                # Pass no tool set rather than an empty one: an empty set would
                # read as "ran nothing" and fail every honest HERMES answer.
                job.result = "".join(chunks)
                verified, reason = self.verifier.check(job.result)

            # Recorded once, after the verifier has run: a HERMES turn that returns
            # text is not the same as a turn that returned a usable answer, and
            # recording it early taught the analyzer that refusals were successes.
            self._record(job, decision.handler, success=verified, started=started)

            if not verified:
                job.status = JobStatus.FAILED
                job.error = f"Result verification failed: {reason}"
                print("[JOB] Status: failed")
                yield self._done(job)
                return

        except Exception as error:
            yield from self._abort(job, error)
            return

        job.status = JobStatus.COMPLETED
        print("[JOB] Status: completed")
        yield self._done(job)

    def _may_execute(self, surface):
        """Whether this surface is allowed to run things on the machine.

        The web surface is the one that matters: it may be bound to a port, and
        it has no authentication. Everywhere else (the CLI) execution is the
        point of the tool.
        """
        if surface != "web":
            return True

        return bool(self.local_tools)

    def _hermes_turn(self, job, decision, yolo=False):
        """Run one HERMES turn with session pinning and approval handling.

        Returns a dict with ``output``, or ``approval`` when HERMES wants the
        user's consent, or ``error``. Never raises, so a failure is reported the
        same way whether it came from the subprocess or the database.
        """
        session_id = None

        if self.store and self.conversation_id:
            session_id = self.store.get_hermes_session(self.conversation_id)

        started = time.time()
        new_session = None if session_id else self.hermes.latest_session_id()

        try:
            output = self.hermes.run(
                decision.task,
                model=decision.model,
                session_id=session_id,
                yolo=yolo,
            )
        except ApprovalRequired as request:
            # Park the session too: this turn created one, and the approved
            # replay should resume it instead of starting over.
            pinned = self._pin_session(session_id, new_session, yolo=False)

            pending = PendingApproval(
                task=decision.task,
                model=decision.model,
                session_id=pinned,
                conversation_id=self.conversation_id,
                target=request.target,
                description=request.description,
            )

            return {
                "approval": approval_registry.add(pending),
                "error": f"HERMES needs approval: {request.description or 'dangerous action'}",
            }
        except Exception as error:
            self._record(job, "hermes", success=False, started=started)
            return {"error": f"{type(error).__name__}: {error}"}

        self._pin_session(session_id, new_session, yolo=yolo)

        return {"output": output}

    def _pin_session(self, session_id, previous, yolo=False):
        """Bind the session HERMES just opened, and return the effective id.

        A one-shot always opens a fresh session, so the newest one belongs to
        this turn. Skipped when a session was already pinned, or on the approved
        replay where the pin has to stay where it was.
        """
        if session_id or not self.store or not self.conversation_id:
            return session_id

        if yolo:
            return None

        discovered = self._discover_session(previous)

        if not discovered:
            return None

        try:
            self.store.bind_hermes_session(self.conversation_id, discovered)
        except Exception as error:
            print(f"[HERMES] could not pin session: {error}")
            return None

        print(f"[HERMES] pinned session {discovered}")

        return discovered

    def _discover_session(self, previous):
        """The session HERMES created for this turn, if it is not the old one."""
        latest = self.hermes.latest_session_id()

        if latest and latest != previous:
            return latest

        return None

    def _record(self, job, handler, success, started):
        if not self.store or not self.conversation_id:
            return

        try:
            self.store.record_route_outcome(
                self.conversation_id,
                handler,
                job.analysis.action if job.analysis else None,
                job.type.value,
                success,
                (time.time() - started) * 1000,
            )
        except Exception as error:
            # Feedback is best-effort; never fail a completed turn over it.
            print(f"[SAGE] could not record route outcome: {error}")

    def _done(self, job):
        # The approval frame is yielded on its own before this, so it is not
        # repeated here.
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
