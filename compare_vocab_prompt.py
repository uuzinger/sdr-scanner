#!/usr/bin/env python3
"""
compare_vocab_prompt.py

Standalone A/B comparison of faster-whisper transcription with and without
the initial_prompt vocabulary hint from vocab.py. Mirrors worker.py's model
config and transcribe() call exactly. Does NOT touch scanner-worker.service
or the transcripts table — read-only against a folder of .m4a files.

Usage:
    python3 compare_vocab_prompt.py /vol1/sdr-scanner/audio/loudoun/sample/*.m4a
    python3 compare_vocab_prompt.py --dir /vol1/sdr-scanner/audio/loudoun/sample
"""

import argparse
import glob
import os
import sys
from pathlib import Path

from faster_whisper import WhisperModel

from vocab import INITIAL_PROMPT

# ---- config (matches worker.py's env-overridable defaults) ----
MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")


def transcribe(model, audio_path, prompt=None):
    """Same call as worker.py's transcribe(), plus optional initial_prompt."""
    segments, info = model.transcribe(
        audio_path,
        language="en",
        beam_size=1,
        vad_filter=True,
        initial_prompt=prompt,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    conf = getattr(info, "language_probability", None)
    return text, conf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="Audio files to compare")
    parser.add_argument("--dir", help="Directory of .m4a files (non-recursive)")
    args = parser.parse_args()

    files = list(args.files)
    if args.dir:
        files.extend(sorted(glob.glob(str(Path(args.dir) / "*.m4a"))))

    if not files:
        print("No files given. Pass file paths or --dir <folder>.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {MODEL_NAME} ({DEVICE}, {COMPUTE_TYPE})...", file=sys.stderr)
    model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)

    for f in files:
        name = Path(f).name
        baseline, base_conf = transcribe(model, f, prompt=None)
        boosted, boost_conf = transcribe(model, f, prompt=INITIAL_PROMPT)

        print("=" * 100)
        print(f"FILE: {name}")
        print("-" * 100)
        print(f"[baseline] (lang_prob={base_conf}) {baseline}")
        print()
        print(f"[vocab   ] (lang_prob={boost_conf}) {boosted}")
        print()
        if baseline.strip() == boosted.strip():
            print("(identical)")
        print()


if __name__ == "__main__":
    main()
