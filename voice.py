import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

try:
    import numpy as np
except ImportError:
    np = None

import requests

from llm import load_env as _load_env

_load_env()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHATTERBOX_URL = os.environ.get("CHATTERBOX_URL", "http://127.0.0.1:8000/tts")
WAKE_MODEL = os.environ.get(
    "SAGE_WAKE_MODEL", os.path.join(BASE_DIR, "wake_models", "sage.json")
)
WAKE_WORD = os.environ.get("SAGE_WAKE_WORD", "sage")
ELK_CHANCE = float(os.environ.get("SAGE_ELK_CHANCE", "0.6"))
SILENCE_RMS = float(os.environ.get("SAGE_SILENCE_RMS", "0.02"))

SR = 16000
N_FFT, N_MELS = 512, 40

SPEAK_LOCK_UNTIL = 0.0
_fallback_hook = []
_lock = threading.Lock()


def _no_speech_until(seconds):
    global SPEAK_LOCK_UNTIL
    with _lock:
        SPEAK_LOCK_UNTIL = max(SPEAK_LOCK_UNTIL, time.time() + seconds)


def _speech_locked():
    with _lock:
        return time.time() < SPEAK_LOCK_UNTIL


SCEN_DIR = os.path.join(BASE_DIR, "scenarios")


class ScenarioPlayer:
    def __init__(self, dir_path=SCEN_DIR):
        self.dir = dir_path
        self.config = {}
        self._last = {}
        self._cfg_lock = threading.Lock()
        self.reload()

    def reload(self):
        path = os.path.join(self.dir, "scenarios.json")
        with self._cfg_lock:
            if os.path.exists(path):
                try:
                    self.config = json.loads(open(path).read())
                except Exception as e:
                    print(f"[voice] scenarios.json unreadable: {e}")
                    self.config = {}

    def list(self):
        with self._cfg_lock:
            return {
                k: {
                    "phrase": v.get("phrase", ""),
                    "wav": v.get("wav", ""),
                    "rendered": os.path.exists(
                        os.path.join(self.dir, v.get("wav", ""))
                    ),
                }
                for k, v in sorted(self.config.items())
            }

    def play(self, key, force=False):
        with self._cfg_lock:
            entry = self.config.get(key)
        if not entry:
            return False
        gap = float(entry.get("min_gap", 1.0))
        now = time.time()
        last = self._last.get(key, 0.0)
        if not force and gap and now - last < gap:
            return False
        self._last[key] = now
        wav = os.path.join(self.dir, entry.get("wav", ""))
        if os.path.exists(wav):
            with open(wav, "rb") as f:
                return _play(f.read())
        return bool(speak(entry.get("phrase", key)))


SCENES = ScenarioPlayer()


def _mel_filters(n_fft=N_FFT, n_mels=N_MELS, fmin=80, fmax=7600, sr=SR):
    mel = lambda hz: 2595.0 * np.log10(1.0 + hz / 700.0)
    pts = np.linspace(mel(fmin), mel(fmax), n_mels + 2)
    hz = 700.0 * (10.0 ** (pts / 2595.0) - 1.0)
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    filters = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(n_mels):
        f, c, t = bins[i], bins[i + 1], bins[i + 2]
        if c > f:
            filters[i, f:c] = np.linspace(0.0, 1.0, c - f)
        if t > c:
            filters[i, c:t] = np.linspace(1.0, 0.0, t - c)
    return filters


def feature_of(audio):
    filters = _mel_filters()
    frame, hop = int(SR * 0.025), int(SR * 0.010)
    cols = max((len(audio) - frame) // hop + 1, 1)
    t_bins = 10
    bins = np.zeros((t_bins, N_MELS))
    step = max(cols // t_bins, 1)
    for i in range(cols):
        seg = audio[i * hop : i * hop + frame].astype(np.float64)
        en = np.log(
            np.dot(filters, np.abs(np.fft.rfft(seg * np.hanning(frame), n=N_FFT)) ** 2)
            + 1e-10
        )
        bins[min(i // step, t_bins - 1)] += en
    vec = bins.ravel()
    vec = vec - vec.mean()
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 1e-9 else vec


class KeySpotter:
    def __init__(self, model_path=None):
        self.name = WAKE_WORD
        self.window = int(SR * 1.0)
        self.centroid, self.threshold, self.ok = None, 0.0, False
        if model_path and os.path.exists(model_path):
            data = json.loads(open(model_path).read())
            self.centroid = np.array(data["centroid"], dtype=np.float32)
            self.threshold = float(
                os.environ.get(
                    "SAGE_WAKE_THRESHOLD", data.get("threshold", 0.88)
                )
            )
            self.name = data.get("name", self.name)
            self.ok = True

    def score(self, audio):
        if len(audio) < self.window:
            audio = np.pad(audio, (0, self.window - len(audio)))[: self.window]
        else:
            audio = audio[-self.window :]
        vec = feature_of(audio)
        return float(np.dot(vec, self.centroid))


def _chatterbox(text):
    try:
        resp = requests.post(CHATTERBOX_URL, json={"text": text}, timeout=120)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except requests.RequestException:
        pass
    return None


def _wav_duration(wav_bytes):
    import io

    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 4.0


def _play(wav_bytes):
    path = os.path.join(tempfile.gettempdir(), "sage_speech.wav")
    with open(path, "wb") as f:
        f.write(wav_bytes)
    _no_speech_until(_wav_duration(wav_bytes) + 0.6)
    if shutil.which("afplay"):
        subprocess.run(["afplay", path], check=False)
        return True
    return False


def speak(text, on_fallback=None):
    wav = _chatterbox(text)
    if wav and _play(wav):
        return True
    for hook in _fallback_hook:
        try:
            hook(on_fallback)
        except Exception:
            pass
    _no_speech_until(len(text) / 13.0 + 0.5)
    return say(text)


def say(text):
    if shutil.which("say"):
        subprocess.run(["/usr/bin/say", text], check=False)


def speak_reply(tagged):
    spoken, detail = parse_speak_detail(tagged)
    if spoken:
        speak(spoken)
    return detail


def parse_speak_detail(reply):
    if not reply:
        return "", ""
    m = re.search(r"\[SPEAK\]\s*(.*?)\s*\[/SPEAK\]", reply, re.S)
    n = re.search(r"\[NOTE\]\s*(.*?)\s*\[/NOTE\]", reply, re.S)
    spoken = m.group(1).strip() if m else reply.strip()
    detail = n.group(1).strip() if n else ("" if m else reply.strip())
    if not m:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", spoken) if s.strip()]
        spoken = " ".join(sents[:2])
    return spoken, detail


class _STTProc:
    def __init__(self, timeout=240):
        self.timeout = timeout
        self.p = None
        self._stt_lock = threading.Lock()

    def alive(self):
        return bool(self.p and self.p.poll() is None)

    def spawn(self):
        if self.alive():
            return True
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "stt_worker.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        return True

    def _kill(self):
        try:
            if self.p:
                self.p.kill()
        except Exception:
            pass
        self.p = None

    def run(self, path):
        with self._stt_lock:
            for attempt in (0, 1):
                self.spawn()
                try:
                    self.p.stdin.write(path + "\n")
                    self.p.stdin.flush()
                    import selectors

                    sel = selectors.DefaultSelector()
                    sel.register(self.p.stdout, selectors.EVENT_READ)
                    if not sel.select(self.timeout):
                        self._kill()
                        return None
                    line = self.p.stdout.readline()
                    if not line:
                        self._kill()
                        if attempt:
                            return None
                        continue
                    data = json.loads(line)
                    return data.get("text") if data.get("ok") else None
                except Exception:
                    self._kill()
                    if attempt:
                        return None
            return None


STT = _STTProc()


def transcribe(path):
    try:
        return STT.run(path)
    except Exception:
        return None


def prewarm():
    try:
        STT.spawn()
        return "stt-worker subprocess (isolated)"
    except Exception as e:
        return f"STT unavailable: {e}"


class Mic:
    RFC_VOL = dict(SILENCE_RMS=SILENCE_RMS)

    def __init__(self):
        import sounddevice as sd

        self.sd = sd
        self.ring = collections.deque(maxlen=SR * 2)
        self.rec = []
        self.recording = False
        self.stream = None

    def start(self):
        self.stream = self.sd.InputStream(
            samplerate=SR,
            channels=1,
            dtype="int16",
            blocksize=int(SR * 0.2),
            callback=self._cb,
        )
        self.stream.start()

    def _cb(self, indata, frames, time_info, status):
        chunk = indata[:, 0]
        self.ring.extend(chunk)
        if self.recording:
            self.rec.extend(chunk)

    def recent(self, seconds=1.0):
        n = int(SR * seconds)
        samples = list(self.ring)
        arr = np.array(samples, dtype=np.int16) if samples else np.zeros(0, np.int16)
        return arr[-n:]

    def save_rec(self, path):
        audio = np.asarray(self.rec, dtype=np.int16)
        active = np.where(np.abs(audio) > 150)[0]
        if len(active):
            audio = audio[: active[-1] + int(SR * 0.2)]
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(audio.tobytes())
        self.rec = []
        return {"path": path, "seconds": len(audio) / SR}


class VoiceLoop(threading.Thread):
    def __init__(self, on_command, sink=None, elk_chance=ELK_CHANCE):
        super().__init__(daemon=True)
        self.on_command = on_command
        self.sink = sink
        self.elk_chance = elk_chance
        self.spot = KeySpotter(WAKE_MODEL)
        self._shutoff = threading.Event()
        self.state = "off"
        self.sd = None
        self._turn_busy_until = 0.0
        self._burst = np.empty(0, dtype=np.float32)
        self._burst_len = 0
        self._burst_open = False
        self._burst_trail = 0.0
        self._rearm = 0

    def _emit(self, kind, text=""):
        if self.sink:
            try:
                self.sink(kind, text)
            except Exception:
                pass

    def stop(self):
        self._shutoff.set()
        if self.sd:
            try:
                self.sd.stop()
            except Exception:
                pass

    def _detect(self, mic, wake):
        if _speech_locked() or time.time() < self._turn_busy_until:
            return False
        if self._rearm > 0:
            self._rearm -= 1
            return True
        if self.spot.ok:
            frame = np.asarray(mic.recent(0.2), np.float32)
            loud = (
                float(np.sqrt(np.mean(frame ** 2))) / 32768.0 > 0.005
            )
            if loud:
                self._burst_trail = 0.0
                if not self._burst_open:
                    self._burst_open = True
                    self._burst = np.empty(0, dtype=np.float32)
                    self._burst_len = 0
                if len(self._burst) <= int(SR * 1.35):
                    self._burst = np.concatenate([self._burst, frame])
                self._burst_len += len(frame)
                return False
            if not self._burst_open:
                return False
            self._burst_trail += 0.2
            if self._burst_trail < 0.3:
                return False
            self._burst_open = False
            if not (int(SR * 0.25) <= self._burst_len <= int(SR * 1.3)):
                return False
            match = float(feature_of(self._burst) @ self.spot.centroid)
            if match > self.spot.threshold:
                print(
                    f"[voice] wake match '{self.spot.name}' "
                    f"score={match:.3f} burst={self._burst_len / SR:.2f}s "
                    f"(threshold {self.spot.threshold:.2f})"
                )
                self._rearm = 1
                return True
            return False
        buf = mic.recent(1.0)
        rms = float(np.sqrt(np.mean(buf.astype(np.float32) ** 2))) / 32768.0
        if rms < max(self._floor * 1.5, 0.004):
            return False
        if wake:
            preds = wake.predict(buf)
            return max(v for k, v in preds.items()) > 0.5
        return False

    def _measure_floor(self, mic, seconds=1.5):
        samples = []
        for _ in range(int(seconds / 0.2)):
            time.sleep(0.2)
            if self._shutoff.is_set():
                break
            arr = np.asarray(mic.recent(0.2), np.float32)
            if arr.size:
                samples.append(float(np.sqrt(np.mean(arr ** 2))))
        return float(np.mean(samples)) / 32768.0 if samples else SILENCE_RMS

    def _think_hint(self):
        if self.state == "thinking":
            SCENES.play("thinking")

    def _run_turn(self, mic):
        if np is not None and np.random.random() < self.elk_chance:
            self.state = "speaking"
            self._emit("state", "speaking")
            SCENES.play("ack")
        self.state = "listening"
        self._emit("state", "listening")

        self.state = "hearing"
        self._emit("state", "hearing")
        mic.rec = []
        mic.recording = True
        silence = 0
        silence_needed = int(2.0 / 0.2)
        total = 0.0
        last = 0
        stall = 0.0
        stalled = False
        floor = max(SILENCE_RMS, self._floor * 2.0)
        while not self._shutoff.is_set() and total < 20.0:
            time.sleep(0.05)
            n = len(mic.rec)
            if n == last:
                stall += 0.05
                if stall > 3.0:
                    stalled = True
                    break
                continue
            chunk = mic.rec[last:n]
            stall = 0.0
            last = n
            total += len(chunk) / SR
            rms = float(
                np.sqrt(np.mean(np.asarray(chunk, np.float32) ** 2))
            ) / 32768.0
            silence = silence + 1 if rms < floor else 0
            if silence >= silence_needed and total > 1.0:
                break
        mic.recording = False
        if self._shutoff.is_set():
            return
        if stalled:
            SCENES.play("stall")
            self.state = "listening"
            self._emit("state", "listening")
            return
        if total >= 20.0:
            SCENES.play("timeout")

        rec = mic.save_rec("/tmp/sage_cmd.wav")
        self.state = "thinking"
        think = threading.Timer(4.0, self._think_hint)
        think.start()
        try:
            text = transcribe(rec["path"])
        finally:
            think.cancel()
        if not text:
            self.state = "listening"
            self._emit("heard", "")
            SCENES.play("no_command")
            return
        self._emit("heard", text)
        print(f"[voice] recognized: {text}")
        if self.on_command:
            think = threading.Timer(4.0, self._think_hint)
            think.start()
            try:
                self.on_command(text)
            except Exception as e:
                self._emit("tool", f"[voice] {e}")
            finally:
                think.cancel()
        self.state = "listening"
        self._emit("state", "listening")

    def run(self):
        try:
            import sounddevice as _sd
        except ImportError:
            self.state = "disabled"
            self._emit("state", "disabled")
            SCENES.play("disabled")
            print("[voice] disabled: pip install sounddevice numpy")
            return
        sd = self.sd = _sd

        wake = None
        if not self.spot.ok:
            try:
                from openwakeword.model import Model

                wake = Model(enable_speex=False)
                print("[voice] using built-in 'jarvis' (no custom model)")
            except ImportError:
                pass
        if not wake and not self.spot.ok:
            self.state = "disabled"
            self._emit("state", "disabled")
            SCENES.play("disabled")
            print(
                f"[voice] disabled: no '{WAKE_WORD}' model at {WAKE_MODEL}. "
                "Run record_wake_samples.py then train_wake.py."
            )
            return

        mic = Mic()
        try:
            mic.start()
        except Exception as e:
            self.state = "permission"
            self._emit("state", "permission")
            self._emit("tool", f"[voice] mic unavailable: {e}")
            SCENES.play("permission")
            print(f"[voice] mic unavailable: {e}")
            return
        self._floor = max(self._measure_floor(mic), 0.003)
        _fallback_hook.append(
            lambda on_fallback=None: (
                SCENES.play("tts_down"),
                self._emit(
                    "tool", "[voice] TTS unreachable -> macOS say fallback"
                ),
            )
        )
        self.state = "listening"
        self._emit("state", "listening")

        def _warmup():
            try:
                STT.spawn()
                self._emit("tool", "[voice] STT worker ready (isolated)")
            except Exception as e:
                self._emit("tool", f"[voice] STT warmup failed: {e}")

        if not STT.alive():
            threading.Thread(target=_warmup, daemon=True).start()
            self._emit(
                "tool",
                "[voice] starting STT worker (one-time model download may follow)...",
            )

        print(f"[voice] listening for '{self.spot.name}'")
        last_det = False
        while not self._shutoff.is_set():
            try:
                det = self._detect(mic, wake)
                engage = det and last_det
                last_det = det
                if not engage:
                    time.sleep(0.2)
                    continue
                self._turn_busy_until = time.time() + 120.0
                try:
                    self._run_turn(mic)
                finally:
                    self._turn_busy_until = 0.0
                    _no_speech_until(0.9)
                last_det = False
                if self._shutoff.is_set():
                    break
            except Exception:
                self._turn_busy_until = 0.0
                last_det = False
                time.sleep(1)
                continue


def start_voice(on_command, sink=None):
    if not shutil.which("say"):
        print("[voice] skipped: macOS 'say' not found")
        return None
    v = VoiceLoop(on_command, sink=sink)
    v.start()
    return v