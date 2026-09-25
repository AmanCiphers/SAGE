import argparse
import json
import os
from pathlib import Path

import requests

import voice

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCEN_DIR = os.path.join(BASE_DIR, "scenarios")


def render_one(phrase, url):
    resp = requests.post(url, json={"text": phrase}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content


def render_local(phrase, out_wav):
    import subprocess
    import tempfile

    aiff = os.path.join(tempfile.gettempdir(), "sage_scene.aiff")
    subprocess.run(["/usr/bin/say", "-o", aiff, phrase], check=True)
    subprocess.run(
        [
            "/usr/bin/afconvert",
            aiff,
            out_wav,
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            "-c",
            "1",
        ],
        check=True,
    )
    return open(out_wav, "rb").read()


def main():
    ap = argparse.ArgumentParser(
        description="Render scenario prompts to scenarios/*.wav via the TTS server."
    )
    ap.add_argument(
        "--server",
        default=voice.CHATTERBOX_URL,
        help="TTS speech endpoint (default: CHATTERBOX_URL)",
    )
    ap.add_argument("--dry-run", action="store_true", help="just list phrases, render nothing")
    ap.add_argument("--key", help="render only this scenario key")
    ap.add_argument(
        "--local",
        action="store_true",
        help="fallback: synthesize with macOS 'say' instead of the TTS server (16kHz WAV)",
    )
    args = ap.parse_args()

    root = Path(SCEN_DIR)
    root.mkdir(parents=True, exist_ok=True)
    config = json.loads((root / "scenarios.json").read_text())

    if not args.local:
        try:
            probe = requests.post(args.server, json={"text": "test"}, timeout=3)
            ok = probe.status_code == 200 and bool(probe.content)
            print(f"[net] tts server reachable: {args.server} -> HTTP {probe.status_code}")
            if not ok:
                print("[net] ! server answered but returned no audio; check the container")
                return
        except requests.RequestException as e:
            print(f"[net] ! cannot reach TTS server {args.server}: {e.__class__.__name__}")
            print("[net]   tip: is the home box on?  run:  ping <box-ip>")
            print("[net]   tip: is port 8000 open?   run:  nc -vz <box-ip> 8000")
            print("[net]   tip: IP changed?          run:  python3 render_prompts.py --server http://<box-ip>:8000/tts")
            print("[net]   fallback for now:         python3 render_prompts.py --local")
            return

    keys = [args.key] if args.key else list(config)
    rendered, skipped, failed = 0, 0, 0
    for key in keys:
        entry = config[key]
        out = root / entry["wav"]
        print(f"[scene] {key}: {entry['phrase']}")
        if args.dry_run:
            continue
        try:
            if args.local:
                data = render_local(entry["phrase"], str(out))
            else:
                data = render_one(entry["phrase"], args.server)
            out.write_bytes(data)
            rendered += 1
            print(f"        -> {len(data):,} bytes -> {out}")
        except Exception as e:
            failed += 1
            if out.exists():
                print(f"        ! render failed ({e}); keeping {out.name}")
            else:
                print(f"        ! render failed ({e}); no audio yet")
            skipped += 1
    print(f"\n[done] rendered={rendered} kept/skipped={skipped} failed={failed}")
    if failed:
        print("[warn] run again when the TTS server is reachable to fill the gaps.")


if __name__ == "__main__":
    main()