"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Panel from "./panel";
import {
  ENDPOINT,
  MODEL,
  MODEL_SHORT,
  PROMPTS,
  RUNTIME,
  STORE,
  TRANSPORT,
  fmtChars,
  fmtClock,
  fmtElapsed,
  fmtMs,
  fmtStamp,
} from "@/lib/format";

const TONE = {
  in: { text: "text-zinc-100", tag: "text-zinc-500" },
  sage: { text: "text-zinc-400", tag: "text-zinc-700" },
  tool: { text: "text-zinc-600", tag: "text-zinc-800" },
  sys: { text: "text-zinc-600", tag: "text-zinc-800" },
};

const TAG = { in: ">", sage: "sage", tool: "log", sys: "::" };

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
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [apiUp, setApiUp] = useState(true);
  const [stamps, setStamps] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [stats, setStats] = useState(emptyStats);
  const [now, setNow] = useState(() => Date.now());

  const seq = useRef(0);
  const startedAt = useRef(Date.now());
  const logRef = useRef(null);
  const inputRef = useRef(null);

  const push = useCallback((kind, text) => {
    setLines((cur) => [...cur, { id: ++seq.current, kind, text, at: Date.now() }]);
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

    try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const data = await res.json();
      const chunks = Array.isArray(data.events) ? [...data.events] : [];
      if (data.response && chunks[chunks.length - 1] !== data.response) {
        chunks.push(data.response);
      }
      if (!chunks.length) throw new Error("empty response");

      chunks.forEach((text) => push("sage", text));
      outChars = chunks.join("").length;
      status = "OK";
      setApiUp(true);
    } catch (e) {
      push("tool", `[err] ${e instanceof Error ? e.message : String(e)}`);
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
  }, [input, busy, push, focusComposer]);

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
          <span className="hidden md:inline">{MODEL_SHORT}</span>
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
                  <span
                    className={`min-w-0 flex-1 ${TONE[line.kind].text} ${
                      wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre"
                    }`}
                  >
                    {line.kind === "in" ? line.text.replace(/^>\s*/, "") : line.text}
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
              <Row label="model" value={MODEL} tone="text-zinc-400" />
              <Row label="transport" value={TRANSPORT} />
              <Row label="runtime" value={RUNTIME} />
              <Row label="store" value={STORE} />
              <Row label="endpoint" value={ENDPOINT} />
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
          {MODEL_SHORT} · {TRANSPORT} · {STORE}
        </span>
        <span>1 channel · {apiUp ? "linked" : "unlinked"}</span>
      </footer>
    </main>
  );
}
