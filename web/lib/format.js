export const MODEL = "nvidia/nemotron-3-ultra-550b-a55b";
export const MODEL_SHORT = "nemotron-3-ultra";
export const RUNTIME = "fastapi · uvicorn";
export const STORE = "sqlite";
export const ENDPOINT = "POST /chat";
export const TRANSPORT = "nim · https";

export const PROMPTS = [
  "Who are you and what can you do?",
  "Break down a goal I am working towards",
  "Summarise what you remember about me",
];

const pad = (n, w = 2) => String(n).padStart(w, "0");

export const fmtClock = (ms) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

export const fmtStamp = (ms) => {
  const d = new Date(ms);
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

export const fmtElapsed = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(
    s % 60
  )}`;
};

export const fmtMs = (ms) =>
  ms == null ? "—" : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;

export const fmtChars = (n) => (n > 999 ? `${(n / 1000).toFixed(1)}k` : String(n));
