"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type Task = {
  id: string;
  title: string;
  status: string;
  created_at: number;
  updated_at: number;
  progress: string | null;
  result: string | null;
};

type Reminder = {
  id: string;
  title: string;
  due_at: number;
  repeat_interval: number;
  done: number;
  context: string | null;
};

type Status = {
  uptime_s: number;
  channels: string[];
  model: string;
};

type LogLine = { kind: "in" | "sage" | "tool"; text: string };

type VoiceEvent = { id: number; kind: string; text: string };

type VoiceState = { state: string; model: string; tts_url?: string };

const fmtClock = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], { hour12: false });

const fmtUptime = (s: number) =>
  `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m ${String(s % 60).padStart(2, "0")}s`;

const statusMark: Record<string, string> = {
  queued: "[..]",
  running: "[>>]",
  done: "[OK]",
  failed: "[XX]",
};

function Panel({
  title,
  children,
  className = "",
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`flex flex-col border border-zinc-800 bg-zinc-950 ${className}`}
    >
      <header className="border-b border-zinc-800 px-3 py-2 text-xs uppercase tracking-[0.2em] text-zinc-500">
        {title}
      </header>
      <div className="flex-1 overflow-y-auto">{children}</div>
    </section>
  );
}

export default function Page() {
  const [status, setStatus] = useState<Status | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [log, setLog] = useState<LogLine[]>([
    { kind: "sage", text: "system online. awaiting input." },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [rTitle, setRTitle] = useState("");
  const [rIn, setRIn] = useState(60);
  const [apiDown, setApiDown] = useState(false);
  const [voiceMode, setVoiceMode] = useState(false);
  const [voiceState, setVoiceState] = useState("off");
  const [voiceModel, setVoiceModel] = useState("");
  const [voiceTts, setVoiceTts] = useState("");
  const [scenes, setScenes] = useState<string[]>([]);
  const [sceneKey, setSceneKey] = useState("ack");
  const voiceCursorRef = useRef(0);

  const logRef = useRef<HTMLDivElement>(null);
  const historyRef = useRef<LogLine[]>([]);
  historyRef.current = log;

  const append = useCallback((lines: LogLine[]) => {
    setLog((cur) => [...cur, ...lines]);
  }, []);

  const poll = useCallback(
    async (silent = false) => {
      try {
        const [st, ts, rs] = await Promise.all([
          fetch(`${API}/status`).then((r) => r.json()),
          fetch(`${API}/tasks`).then((r) => r.json()),
          fetch(`${API}/reminders`).then((r) => r.json()),
        ]);
        setStatus(st);
        setTasks(ts);
        setReminders(rs);
        if (apiDown) setApiDown(false);
      } catch {
        setApiDown(true);
        if (!silent)
          setLog((cur) => [
            ...cur,
            { kind: "tool", text: `[err] api unreachable at ${API}` },
          ]);
      }
    },
    [apiDown]
  );

  useEffect(() => {
    poll(true);
    const id = setInterval(() => poll(true), 3000);
    return () => clearInterval(id);
  }, [poll]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [log]);

  useEffect(() => {
    fetch(`${API}/voice/scenarios`)
      .then((r) => r.json())
      .then((d) => {
        const keys = d?.scenarios ? Object.keys(d.scenarios) : [];
        setScenes(keys);
        if (keys.length) setSceneKey(keys[0]);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!voiceMode) return;
    let alive = true;
    const tick = async () => {
      try {
        const [st, ev] = await Promise.all([
          fetch(`${API}/voice/state`).then((r) => r.json()),
          fetch(`${API}/voice/events?after=${voiceCursorRef.current}`).then((r) =>
            r.json(),
          ),
        ]);
        if (!alive) return;
        setVoiceState(st.state);
        setVoiceModel(st.model);
        setVoiceTts(st.tts_url);
        for (const e of (ev.events ?? []) as VoiceEvent[]) {
          voiceCursorRef.current = Math.max(voiceCursorRef.current, e.id);
          if (e.kind === "heard")
            append([{ kind: "in", text: `(voice) ${e.text}` }]);
          else if (e.kind === "speaking")
            append([{ kind: "sage", text: `(voice) ${e.text}` }]);
          else if (e.kind === "detail")
            append([{ kind: "sage", text: e.text }]);
          else
            append([
              {
                kind: "tool",
                text: `[voice ${e.kind}]${e.text ? ` ${e.text}` : ""}`,
              },
            ]);
        }
      } catch {
        /* server restarting */
      }
    };
    tick();
    const id = setInterval(tick, 400);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [voiceMode, append]);

  const toggleVoice = async () => {
    const next = !voiceMode;
    try {
      const res = await fetch(`${API}/voice/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on: next }),
      });
      const data = await res.json();
      setVoiceMode(next);
      setVoiceState(data.state);
      append([{ kind: "tool", text: `[voice] ${data.state}` }]);
    } catch (e) {
      append([
        { kind: "tool", text: `[err] voice toggle ${e instanceof Error ? e.message : e}` },
      ]);
    }
  };

  const send = async () => {
    const msg = input.trim();
    if (!msg || busy) return;
    setInput("");
    setBusy(true);
    append([
      { kind: "in", text: `> ${msg}` },
      { kind: "tool", text: `[sending to ${status?.model ?? "nim"}]` },
    ]);
    try {
      const res = await fetch(`${API}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const lines = (data.events ?? []) as string[];
      if (data.reply && lines[lines.length - 1] !== data.reply)
        lines.push(data.reply);
      append(lines.map((text) => ({ kind: "sage", text })));
    } catch (e) {
      append([
        { kind: "tool", text: `[err] ${e instanceof Error ? e.message : e}` },
      ]);
    } finally {
      setBusy(false);
      poll(true);
    }
  };

  const addReminder = async () => {
    if (!rTitle.trim()) return;
    try {
      await fetch(`${API}/reminders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: rTitle.trim(),
          due_in_seconds: Number(rIn) || 60,
        }),
      });
      setRTitle("");
      poll(true);
    } catch (e) {
      append([
        { kind: "tool", text: `[err] ${e instanceof Error ? e.message : e}` },
      ]);
    }
  };

  const running = tasks.filter((t) => t.status === "running").length;

  return (
    <main className="scanlines relative mx-auto flex min-h-screen max-w-[1500px] flex-col gap-3 p-4">
      <header className="flex items-center justify-between border border-zinc-800 bg-zinc-950 px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="text-sm font-bold tracking-[0.3em] text-zinc-100">
            SAGE
          </span>
          <span className="text-xs text-zinc-600">:: CONTROL</span>
        </div>
        <div className="hidden items-center gap-4 text-[11px] text-zinc-500 md:flex">
          {status && (
            <>
              <span className="text-zinc-400">{status.model}</span>
              <span>
                up {status.uptime_s >= 0 ? fmtUptime(status.uptime_s) : "--"}
              </span>
              <span>{status.channels.join(" / ")}</span>
            </>
          )}
          <span className={`${apiDown ? "text-zinc-400" : "text-zinc-600"}`}>
            {apiDown ? "API DOWN" : "API OK"}
          </span>
        </div>
      </header>

      <div className="grid flex-1 grid-cols-1 gap-3 lg:grid-cols-[1fr_380px]">
        <Panel title="terminal" className="min-h-[360px] lg:min-h-0">
          <div
            ref={logRef}
            className="h-[52vh] overflow-y-auto px-4 py-3 text-[13px] leading-relaxed lg:h-[calc(100vh-180px)]"
          >
            {log.map((line, i) => (
              <div
                key={i}
                className={
                  line.kind === "in"
                    ? "text-zinc-100"
                    : line.kind === "tool"
                      ? "text-zinc-600"
                      : "text-zinc-400"
                }
              >
                {line.kind === "in" && (
                  <span className="mr-2 text-zinc-500">&gt;</span>
                )}
                {line.kind === "sage" && (
                  <span className="mr-2 text-zinc-700">sage</span>
                )}
                <span className="whitespace-pre-wrap break-words">
                  {line.text}
                </span>
              </div>
            ))}
            {busy && (
              <div className="mt-1 text-zinc-600">
                <span className="inline-block h-3 w-2 animate-blink bg-zinc-400" />
              </div>
            )}
          </div>
          <div className="flex items-center gap-2 border-t border-zinc-800 px-4 py-3">
            <span className="text-zinc-500">&gt;</span>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder={busy ? "sage is working..." : "talk to sage"}
              disabled={busy}
              className="w-full bg-transparent text-sm text-zinc-100 outline-none placeholder:text-zinc-700 disabled:opacity-50"
              spellCheck={false}
              autoFocus
            />
          </div>
        </Panel>

        <div className="flex flex-col gap-3">
          <Panel title="voice">
            <div className="space-y-2 px-3 py-3 text-[12px]">
              <div className="flex items-center justify-between">
                <span
                  className={
                    voiceState === "listening"
                      ? "text-zinc-300"
                      : voiceState === "hearing"
                        ? "animate-pulse text-zinc-100"
                        : voiceState === "thinking" || voiceState === "speaking"
                          ? "text-zinc-400"
                          : "text-zinc-700"
                  }
                >
                  {voiceMode ? voiceState.toUpperCase() : "OFF"}
                </span>
                <button
                  onClick={toggleVoice}
                  className={`border px-2 py-1 text-[11px] ${
                    voiceMode
                      ? "border-zinc-500 text-zinc-100 hover:border-zinc-300"
                      : "border-zinc-800 text-zinc-500 hover:border-zinc-500 hover:text-zinc-200"
                  }`}
                >
                  {voiceMode ? "DEACTIVATE" : "ACTIVATE"}
                </button>
              </div>
              <div className="truncate text-[10px] text-zinc-700" title={voiceModel}>
                {voiceModel || "no wake model"}
              </div>
              <div className="truncate text-[10px] text-zinc-700" title={voiceTts}>
                tts: {voiceTts || "—"}
              </div>
              <div className="text-[10px] text-zinc-700">
                say &quot;sage&quot; to wake · silence ends the rest
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={sceneKey}
                  onChange={(e) => setSceneKey(e.target.value)}
                  disabled={scenes.length === 0}
                  className="w-full bg-black border border-zinc-800 px-2 py-1 text-[11px] text-zinc-300 outline-none disabled:opacity-40"
                >
                  {scenes.length === 0 && <option value="">no scenarios</option>}
                  {scenes.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() =>
                    fetch(`${API}/voice/scenario`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ key: sceneKey, force: true }),
                    }).catch(() => {})
                  }
                  disabled={scenes.length === 0}
                  className="shrink-0 border border-zinc-800 px-2 py-1 text-[11px] text-zinc-500 hover:border-zinc-500 hover:text-zinc-200 disabled:opacity-40"
                >
                  PLAY
                </button>
              </div>
            </div>
          </Panel>

          <Panel title="tasks">
            <div className="space-y-2 px-3 py-3 text-[12px]">
              {tasks.length === 0 && (
                <div className="text-zinc-700">
                  no tasks. say &quot;hire a worker to ...&quot;
                </div>
              )}
              {tasks.map((t) => (
                <div
                  key={t.id}
                  className="rounded-sm border border-zinc-900 bg-black px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-zinc-300">{t.title}</span>
                    <span className="shrink-0 text-zinc-500">
                      {statusMark[t.status] ?? `[${t.status}]`}
                    </span>
                  </div>
                  <div className="mt-1 truncate text-[11px] text-zinc-600">
                    {t.progress || "—"}
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[10px] text-zinc-700">
                    <span>#{t.id}</span>
                    <span>{fmtClock(t.updated_at)}</span>
                  </div>
                </div>
              ))}
            </div>
            <div className="border-t border-zinc-800 px-3 py-2 text-[11px] text-zinc-600">
              {tasks.length} total · {running} active
            </div>
          </Panel>

          <Panel title="reminders">
            <div className="space-y-2 px-3 py-3 text-[12px]">
              {reminders.length === 0 && (
                <div className="text-zinc-700">none scheduled</div>
              )}
              {reminders.map((r) => (
                <div
                  key={r.id}
                  className="flex items-center justify-between gap-2 rounded-sm border border-zinc-900 bg-black px-3 py-2"
                >
                  <div className="min-w-0">
                    <div
                      className={`truncate ${r.done ? "text-zinc-700 line-through" : "text-zinc-300"}`}
                    >
                      {r.title}
                    </div>
                    <div className="text-[10px] text-zinc-600">
                      {fmtClock(r.due_at)}
                      {r.repeat_interval > 0 &&
                        ` · every ${r.repeat_interval}s`}
                    </div>
                  </div>
                  <span className="shrink-0 text-zinc-600">
                    {r.done ? "[DONE]" : "[..]"}
                  </span>
                </div>
              ))}
            </div>
            <div className="flex gap-2 border-t border-zinc-800 px-3 py-2">
              <input
                value={rTitle}
                onChange={(e) => setRTitle(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addReminder()}
                placeholder="watch the demo reels"
                className="w-full bg-transparent text-[12px] text-zinc-100 outline-none placeholder:text-zinc-700"
                spellCheck={false}
              />
              <input
                value={rIn}
                onChange={(e) => setRIn(Number(e.target.value) || 0)}
                type="number"
                min={1}
                className="w-16 bg-transparent text-right text-[12px] text-zinc-500 outline-none"
                title="seconds from now"
              />
              <button
                onClick={addReminder}
                className="border border-zinc-800 px-2 py-1 text-[11px] text-zinc-400 hover:border-zinc-500 hover:text-zinc-100"
              >
                ADD
              </button>
            </div>
          </Panel>
        </div>
      </div>

      <footer className="flex items-center justify-between border border-zinc-800 bg-zinc-950 px-4 py-2 text-[10px] text-zinc-700">
        <span>nvidia nim · nemotron · sqlite</span>
        <span>
          {status ? `${status.channels.length} channels · poll 3s` : "offline"}
        </span>
      </footer>
    </main>
  );
}