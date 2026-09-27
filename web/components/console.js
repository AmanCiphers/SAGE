"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Panel from "./panel";
import {
  FALLBACK_INFO,
  PROMPTS,
  fmtChars,
  fmtClock,
  fmtElapsed,
  fmtMs,
  fmtStamp,
  shortModel,
} from "@/lib/format";

const TONE = {
  in: { text: "text-zinc-100", tag: "text-zinc-500" },
  sage: { text: "text-zinc-400", tag: "text-zinc-700" },
  tool: { text: "text-zinc-600", tag: "text-zinc-800" },
  sys: { text: "text-zinc-600", tag: "text-zinc-800" },
};

const TAG = { in: ">", sage: "sage", tool: "log", sys: "::" };

// SAGE answers in markdown, so the tape has to render it rather than show the
// raw "#" and "**". Only applied to sealed lines: re-parsing on every stream
// delta would be wasted work and makes the text jump while it types.
const MD = {
  h1: ({ children }) => <h1 className="mt-3 mb-1 text-[15px] font-semibold text-zinc-100">{children}</h1>,
  h2: ({ children }) => <h2 className="mt-3 mb-1 text-[14px] font-semibold text-zinc-100">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-2 mb-1 text-[13px] font-semibold text-zinc-200">{children}</h3>,
  h4: ({ children }) => <h4 className="mt-2 mb-1 text-[13px] font-medium text-zinc-300">{children}</h4>,
  p: ({ children }) => <p className="my-1 leading-relaxed">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-zinc-200">{children}</strong>,
  em: ({ children }) => <em className="italic text-zinc-300">{children}</em>,
  del: ({ children }) => <del className="text-zinc-600">{children}</del>,
  ul: ({ children }) => <ul className="my-1 list-disc pl-5 space-y-0.5 marker:text-zinc-600">{children}</ul>,
  ol: ({ children }) => <ol className="my-1 list-decimal pl-5 space-y-0.5 marker:text-zinc-600">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  hr: () => <hr className="my-2 border-zinc-800" />,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-zinc-300 underline underline-offset-2 hover:text-zinc-100"
    >
      {children}
    </a>
  ),
  code: ({ children, className }) =>
    className ? (
      <code className="rounded bg-zinc-900 px-1 py-0.5 text-[12px] text-zinc-300">{children}</code>
    ) : (
      <code className="rounded bg-zinc-900 px-1 py-0.5 text-[12px] text-zinc-300">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded border border-zinc-800 bg-zinc-950 p-2 text-[12px] leading-relaxed text-zinc-300">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full border-collapse text-[12px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="border-b border-zinc-700">{children}</thead>,
  th: ({ children }) => <th className="px-2 py-1 text-left font-semibold text-zinc-300">{children}</th>,
  td: ({ children }) => <td className="border-t border-zinc-800 px-2 py-1 align-top text-zinc-400">{children}</td>,
  blockquote: ({ children }) => (
    <blockquote className="my-1 border-l-2 border-zinc-700 pl-3 text-zinc-500">{children}</blockquote>
  ),
};

const bootLines = () => [
  { id: 0, kind: "sys", text: "sage online. personal intelligence core loaded.", at: Date.now() },
];

const emptyStats = () => ({
  turns: 0,
  inChars: 0,
  outChars: 0,
  lastMs: null,
  lastStatus: "--",
});

function Chip({ active, onClick, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`border px-2 py-1 text-[10px] uppercase tracking-[0.15em] transition-colors ${
        active
          ? "border-zinc-600 text-zinc-200"
          : "border-zinc-900 text-zinc-600 hover:border-zinc-700 hover:text-zinc-400"
      }`}
    >
      {children}
    </button>
  );
}

function Row({ label, value, tone = "text-zinc-300" }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="shrink-0 text-[10px] uppercase tracking-[0.15em] text-zinc-600">
        {label}
      </span>
      <span className={`truncate text-[11px] ${tone}`}>{value}</span>
    </div>
  );
}

export default function Console() {
  const [lines, setLines] = useState(bootLines);
  const [info, setInfo] = useState(FALLBACK_INFO);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [apiUp, setApiUp] = useState(true);
  const [stamps, setStamps] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [stats, setStats] = useState(emptyStats);
  const [approval, setApproval] = useState(null);
  const [now, setNow] = useState(() => Date.now());

  const seq = useRef(0);
  const startedAt = useRef(Date.now());
  const logRef = useRef(null);
  const inputRef = useRef(null);

  const push = useCallback((kind, text) => {
    setLines((cur) => [
      ...cur,
      { id: ++seq.current, kind, text, at: Date.now(), sealed: false },
    ]);
  }, []);

  const append = useCallback((kind, text) => {
    setLines((cur) => {
      const last = cur[cur.length - 1];

      if (!last || last.kind !== kind || last.sealed) {
        return [...cur, { id: ++seq.current, kind, text, at: Date.now(), sealed: false }];
      }

      return [...cur.slice(0, -1), { ...last, text: last.text + text }];
    });
  }, []);

  const seal = useCallback((kind) => {
    setLines((cur) => {
      const last = cur[cur.length - 1];

      if (!last || last.kind !== kind) return cur;

      return [...cur.slice(0, -1), { ...last, sealed: true }];
    });
  }, []);

  const focusComposer = useCallback(() => inputRef.current?.focus(), []);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let alive = true;
    const probe = async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (alive) setApiUp(res.ok);
      } catch {
        if (alive) setApiUp(false);
      }
    };
    probe();
    const id = setInterval(probe, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let alive = true;

    fetch("/info", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (alive && data) setInfo((cur) => ({ ...cur, ...data }));
      })
      .catch(() => {});

    return () => {
      alive = false;
    };
  }, []);

  // A reminder that fires while the request stream is closed has no other way
  // to reach the user, so poll for the ones the server recorded.
  useEffect(() => {
    let alive = true;

    const poll = async () => {
      try {
        const res = await fetch("/notifications", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();

        if (!alive || !data.notifications?.length) return;

        for (const note of data.notifications) {
          // "sys" is a real line kind; TONE/TAG have no "notice" entry and an
          // unknown kind would throw during render. Markdown is only rendered
          // on sealed sage lines, so this stays plain text.
          push("sys", `${note.kind}: ${note.body}`);
        }
      } catch {
        /* the console stays usable when the server is restarting */
      }
    };

    const id = setInterval(poll, 5000);
    poll();

    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [push]);

  useEffect(() => {
    if (!autoScroll) return;
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines, busy, autoScroll]);

  const send = useCallback(async () => {
    const msg = input.trim();
    if (!msg || busy) return;

    setInput("");
    setBusy(true);
    push("in", `> ${msg}`);

    const started = performance.now();
    let outChars = 0;
    let status = "ERR";
    let sawText = false;
    let sawError = false;
    let sawDone = false;
    let sawNotice = false;
    let sawApproval = false;

    const onFrame = (payload) => {
      // A parked destructive action: nothing has run yet, and the user has to
      // answer before anything does. Handled before the generic branches so it
      // cannot be mistaken for an error.
      if (payload.approval) {
        sawApproval = true;
        seal("sage");
        setApproval(payload.approval);
        push("sys", `:: approval required · ${payload.approval.description || payload.approval.target || ""}`);
        return;
      }

      if (payload.stage) {
        if (payload.stage === "tool" && payload.name) {
          push("tool", `[call] ${payload.name}(${payload.arguments || ""})`);
          return;
        }

        const detail = payload.handler ? ` · ${payload.handler}/${payload.type}` : "";
        const why = payload.reason ? ` · ${payload.reason}` : "";
        push("sys", `:: ${payload.stage}${detail}${why}`);
        return;
      }

      if (payload.tool) {
        const mark = payload.ok === false ? "fail" : "ok";
        push("tool", `[${mark}] ${payload.tool} · ${payload.summary || ""}`);
        return;
      }

      if (payload.notice) {
        sawNotice = true;
        push("sys", `:: ${payload.notice}`);
        return;
      }

      if (payload.error) {
        sawError = true;
        push("tool", `[err] ${payload.error}`);
        seal("sage");
        return;
      }

      if (typeof payload.delta === "string" && payload.delta) {
        append("sage", payload.delta);
        outChars += payload.delta.length;
        sawText = true;
      }

      if (payload.done) {
        sawDone = true;
        seal("sage");
      }
    };

    try {
      const res = await fetch("/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (!res.body) throw new Error("streaming unsupported by this browser");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;

          try {
            onFrame(JSON.parse(line.slice(6)));
          } catch {
            /* ignore malformed frame */
          }
        }
      }

      if (sawApproval) {
        // The turn stopped on a question, not on a broken stream.
        status = "OK";
      } else if (!sawText && !sawError) {
        throw new Error("empty response");
      } else if (!sawDone) {
        sawError = true;
        push("tool", "[err] stream ended early — reply may be incomplete");
        seal("sage");
        status = "ERR";
      } else {
        status = sawNotice ? "PARTIAL" : "OK";
      }

      setApiUp(true);
    } catch (e) {
      push("tool", `[err] ${e instanceof Error ? e.message : String(e)}`);
      seal("sage");
      setApiUp(false);
    } finally {
      const ms = performance.now() - started;
      setStats((cur) => ({
        turns: cur.turns + 1,
        inChars: cur.inChars + msg.length,
        outChars: cur.outChars + outChars,
        lastMs: ms,
        lastStatus: status,
      }));
      setBusy(false);
      focusComposer();
    }
  }, [input, busy, push, append, seal, focusComposer]);

  const answerApproval = useCallback(
    async (approved) => {
      if (!approval) return;

      const pending = approval;
      setApproval(null);
      setBusy(true);
      push("in", `> ${approved ? "yes" : "no"}`);

      try {
        const res = await fetch("/chat/approve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: pending.id, approved }),
        });

        const data = await res.json();

        if (!res.ok || data.status === "failed") {
          push("tool", `[err] ${data.error || `HTTP ${res.status}`}`);
          return;
        }

        push("sage", data.response || (approved ? "Approved." : "Declined."));
        setStats((cur) => ({ ...cur, turns: cur.turns + 1, lastStatus: "OK" }));
        setApiUp(true);
      } catch (e) {
        push("tool", `[err] ${e instanceof Error ? e.message : String(e)}`);
        setApiUp(false);
      } finally {
        setBusy(false);
        focusComposer();
      }
    },
    [approval, push, focusComposer],
  );

  const clear = useCallback(() => {
    setLines(bootLines());
    setStats(emptyStats());
    focusComposer();
  }, [focusComposer]);

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const showWelcome = lines.length <= 1;

  return (
    <main className="scanlines vignette relative mx-auto flex min-h-screen max-w-[1500px] flex-col gap-3 p-4">
      <header className="flex items-center justify-between border border-zinc-800 bg-zinc-950 px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="grid h-6 w-6 place-items-center bg-zinc-100 text-[12px] font-bold text-black">
            S
          </span>
          <span className="text-sm font-bold tracking-[0.3em] text-zinc-100">SAGE</span>
          <span className="hidden text-xs text-zinc-600 sm:inline">:: PERSONAL INTELLIGENCE</span>
        </div>
        <div className="flex items-center gap-4 text-[11px] text-zinc-500">
          <span className="hidden md:inline">{shortModel(info.model)}</span>
          <span className="hidden md:inline">up {fmtElapsed(now - startedAt.current)}</span>
          <span>{fmtClock(now)}</span>
          <span className={apiUp ? "text-emerald-500/70" : "text-rose-500/80"}>
            {apiUp ? "API OK" : "API DOWN"}
          </span>
        </div>
      </header>

      <div className="grid flex-1 grid-cols-1 gap-3 lg:grid-cols-[1fr_340px]">
        <Panel title="terminal" meta={`${stats.turns} turns`} className="min-h-[420px] lg:min-h-0">
          <div
            ref={logRef}
            className="h-[54vh] overflow-y-auto px-4 py-3 text-[13px] leading-relaxed lg:h-[calc(100vh-190px)]"
          >
            {showWelcome && (
              <div className="mb-6 border border-zinc-900 bg-black px-4 py-6 text-center">
                <span className="mx-auto mb-4 grid h-11 w-11 place-items-center bg-zinc-100 text-lg font-bold text-black">
                  S
                </span>
                <h2 className="text-base font-medium tracking-[0.2em] text-zinc-200">SAGE</h2>
                <p className="mt-2 text-[12px] text-zinc-600">
                  Personal intelligence. Ask anything, or describe a goal.
                </p>
                <div className="mt-5 flex flex-wrap justify-center gap-2">
                  {PROMPTS.map((p) => (
                    <Chip
                      key={p}
                      onClick={() => {
                        setInput(p);
                        focusComposer();
                      }}
                    >
                      {p}
                    </Chip>
                  ))}
                </div>
              </div>
            )}

            <div className="tape">
              {lines.map((line) => (
                <div key={line.id} className="flex gap-2">
                  {stamps && (
                    <span className="w-[74px] shrink-0 select-none text-[10px] leading-[1.9] text-zinc-800">
                      {fmtStamp(line.at)}
                    </span>
                  )}
                  <span
                    className={`w-9 shrink-0 select-none text-right text-[11px] leading-[1.9] ${
                      TONE[line.kind].tag
                    }`}
                  >
                    {TAG[line.kind]}
                  </span>
                  <span className={`min-w-0 flex-1 ${TONE[line.kind].text}`}>
                    {line.kind === "sage" && line.sealed ? (
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>
                        {line.text}
                      </ReactMarkdown>
                    ) : (
                      <span
                        className={
                          wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre"
                        }
                      >
                        {line.kind === "in" ? line.text.replace(/^>\s*/, "") : line.text}
                      </span>
                    )}
                  </span>
                </div>
              ))}
            </div>

            {busy && (
              <div className="mt-1 flex gap-2 text-[13px] text-zinc-600">
                <span className="w-9 shrink-0 text-right text-[11px] text-zinc-800">sage</span>
                <span className="inline-block h-3.5 w-2 animate-blink bg-zinc-400" />
              </div>
            )}
          </div>

          {approval && (
            <div className="border-t border-amber-500/40 bg-amber-500/5 px-4 py-3">
              <div className="text-[11px] tracking-[0.2em] text-amber-400/80">
                APPROVAL REQUIRED
              </div>
              <div className="mt-1 text-[13px] leading-relaxed text-zinc-200">
                {approval.description || approval.target}
              </div>
              {approval.target && approval.target !== approval.description && (
                <pre className="mt-2 overflow-x-auto border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[12px] text-zinc-300">
                  {approval.target}
                </pre>
              )}
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => answerApproval(true)}
                  className="border border-amber-400/60 px-3 py-1 text-[12px] text-amber-200 hover:bg-amber-400/10 disabled:opacity-50"
                >
                  allow once
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => answerApproval(false)}
                  className="border border-zinc-700 px-3 py-1 text-[12px] text-zinc-400 hover:bg-zinc-800 disabled:opacity-50"
                >
                  decline
                </button>
              </div>
            </div>
          )}

          <div className="composer flex items-start gap-2 border-t border-zinc-800 px-4 py-3">
            <span className="pt-1.5 text-zinc-500">&gt;</span>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              rows={1}
              disabled={busy}
              spellCheck={false}
              placeholder={busy ? "sage is working..." : "talk to sage  ·  enter to send  ·  shift+enter for newline"}
              className="max-h-40 w-full resize-none bg-transparent py-1.5 text-sm leading-relaxed text-zinc-100 caret-zinc-100 outline-none placeholder:text-zinc-700 disabled:opacity-50"
            />
            <button
              type="button"
              onClick={send}
              disabled={busy || !input.trim()}
              className="mt-0.5 shrink-0 border border-zinc-700 px-3 py-1.5 text-[11px] uppercase tracking-[0.2em] text-zinc-200 transition-colors hover:border-zinc-400 disabled:border-zinc-900 disabled:text-zinc-700"
            >
              {busy ? "..." : "send"}
            </button>
          </div>
        </Panel>

        <div className="flex flex-col gap-3">
          <Panel title="session">
            <div className="space-y-2 px-3 py-3">
              <Row label="turns" value={String(stats.turns)} />
              <Row label="elapsed" value={fmtElapsed(now - startedAt.current)} />
              <Row label="inbound" value={`${fmtChars(stats.inChars)} ch`} />
              <Row label="outbound" value={`${fmtChars(stats.outChars)} ch`} />
              <Row
                label="state"
                value={busy ? "WORKING" : "IDLE"}
                tone={busy ? "text-zinc-100" : "text-zinc-500"}
              />
            </div>
          </Panel>

          <Panel title="telemetry">
            <div className="space-y-2 px-3 py-3">
              <Row label="model" value={info.model} tone="text-zinc-400" />
              <Row label="transport" value={info.transport} />
              <Row label="runtime" value={info.runtime} />
              <Row label="store" value={info.store} />
              <Row label="endpoint" value={info.endpoint} />
              <Row
                label="last call"
                value={`${stats.lastStatus} · ${fmtMs(stats.lastMs)}`}
                tone={stats.lastStatus === "OK" ? "text-emerald-500/70" : "text-zinc-500"}
              />
            </div>
          </Panel>

          <Panel title="display">
            <div className="flex flex-wrap gap-2 px-3 py-3">
              <Chip active={stamps} onClick={() => setStamps((v) => !v)}>
                stamps
              </Chip>
              <Chip active={wrap} onClick={() => setWrap((v) => !v)}>
                wrap
              </Chip>
              <Chip active={autoScroll} onClick={() => setAutoScroll((v) => !v)}>
                autoscroll
              </Chip>
              <Chip active={false} onClick={clear}>
                clear
              </Chip>
            </div>
          </Panel>

          <Panel title="help" className="hidden lg:flex">
            <ul className="space-y-1.5 px-3 py-3 text-[11px] text-zinc-600">
              <li>
                <span className="text-zinc-700">&gt;</span> your message
              </li>
              <li>
                <span className="text-zinc-700">sage</span> model reply
              </li>
              <li>
                <span className="text-zinc-700">::</span> system notice
              </li>
              <li>
                <span className="text-zinc-700">log</span> transport + errors
              </li>
            </ul>
          </Panel>
        </div>
      </div>

      <footer className="flex items-center justify-between border border-zinc-800 bg-zinc-950 px-4 py-2 text-[10px] text-zinc-700">
        <span>
          {shortModel(info.model)} · {info.transport} · {info.store}
        </span>
        <span>1 channel · {apiUp ? "linked" : "unlinked"}</span>
      </footer>
    </main>
  );
}
