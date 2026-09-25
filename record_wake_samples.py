import argparse
import os
import wave
from pathlib import Path

import numpy as np

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wake_data", "sage")


def record_one(path, seconds=1.0):
    import sounddevice as sd

    sr = 16000
    frames = int(sr * seconds)
    print(f"[rec] saying/waiting {seconds}s... (say '{os.environ.get('SAGE_WAKE_WORD', 'sage')}'")
    audio = sd.rec(frames, samplerate=sr, channels=1, dtype="int16")
    sd.wait()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(audio.tobytes())
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    print(f"[rec] saved {path} (rms={rms:.2f})")
    return rms > 600


def main():
    ap = argparse.ArgumentParser(description="Record wake-word training samples.")
    ap.add_argument("--count", type=int, default=15, help="samples to record")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="output directory")
    args = ap.parse_args()

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    taken = 0
    skipped = 0
    while taken < args.count:
        name = f"sage_{taken:02d}.wav"
        path = out / name
        if path.exists():
            taken += 1
            continue
        ok = record_one(str(path))
        if ok:
            taken += 1
            print(f"[rec] {taken}/{args.count}")
        else:
            path.unlink(missing_ok=True)
            skipped += 1
            if skipped > 5:
                print("[rec] too quiet repeatedly; stop whispering sir.")
                skipped = 0
    print(f"\ndone. {args.count} samples in {out}")
    print(f"train with: python3 train_wake.py --data {out}")


if __name__ == "__main__":
    main()