import argparse
import json
import os
import wave
from pathlib import Path

import numpy as np

import voice

DEFAULT_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wake_data", "sage")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wake_models", "sage.json")


def load_clip(path):
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    if sr != voice.SR:
        ratio = voice.SR / sr
        n = int(len(audio) * ratio)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return audio


def main():
    ap = argparse.ArgumentParser(description="Train the custom 'sage' wake-word spotter.")
    ap.add_argument("--data", default=DEFAULT_DATA, help="dir of recorded samples")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output model path")
    ap.add_argument("--threshold", type=float, default=0.88, help="cosine similarity cutoff")
    args = ap.parse_args()

    clips = sorted(Path(args.data).glob("*.wav"))
    if not clips:
        raise SystemExit(f"no samples in {args.data}. Run record_wake_samples.py first.")

    feats = []
    for p in clips:
        audio = load_clip(p)
        vec = voice.feature_of(audio)
        feats.append(vec)
        print(f"[train] {p.name}: {p.stat().st_size} bytes")
    feats = np.array(feats)
    centroid = feats.mean(axis=0)
    norm = np.linalg.norm(centroid)
    centroid = (centroid / norm).astype(float).tolist() if norm > 1e-9 else centroid.tolist()

    best = max(float(np.dot(feats[i], feats.mean(axis=0) / np.linalg.norm(feats.mean(axis=0)))) for i in range(len(feats)))
    worst = min(float(np.dot(feats[i], feats.mean(axis=0) / np.linalg.norm(feats.mean(axis=0)))) for i in range(len(feats)))
    print(f"[train] {len(feats)} samples, self-similarity {worst:.3f}..{best:.3f}")
    if worst < 0.75:
        print("[train] note: samples vary a lot; consider a higher-count retrain or lower threshold.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "name": voice.WAKE_WORD,
        "centroid": centroid,
        "threshold": args.threshold,
        "sr": voice.SR,
        "window_s": 1.0,
    }))
    print(f"[train] model written to {out} (threshold {args.threshold})")
    print(f"[train] restart Sage (SAGE_VOICE=1) to use '{voice.WAKE_WORD}'.")


if __name__ == "__main__":
    main()