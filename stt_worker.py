import json
import os
import sys

import numpy as np
import wave


def load_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    arr = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
    return arr, sr


model = None
engine = None


def ensure():
    global model, engine
    if model is not None:
        return
    try:
        from faster_whisper import WhisperModel

        engine = "faster-whisper"
        model = WhisperModel(
            os.environ.get("SAGE_WHISPER_MODEL", "small"),
            device="cpu",
            compute_type="default",
        )
    except ImportError:
        import whisper

        engine = "openai-whisper"
        model = whisper.load_model(os.environ.get("SAGE_WHISPER_MODEL", "small"))


def transcribe(path):
    arr, _sr = load_wav(path)
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2))) if arr.size else 0.0
    if not (0 < rms < 0.5) or not np.isfinite(rms):
        return None
    ensure()
    if engine == "faster-whisper":
        segs, _info = model.transcribe(
            path, language="en", vad_filter=True, beam_size=1
        )
        text = " ".join(s.text.strip() for s in segs).strip()
    else:
        text = model.transcribe(
            path, language="en", condition_on_previous_text=False
        )["text"].strip()
    return text or None


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        text = transcribe(line)
        print(json.dumps({"ok": True, "engine": engine or "idle", "text": text}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
    sys.stdout.flush()