#!/usr/bin/env python3
"""
This script is a WIP workflow, that tries to detect and trim ads from downloaded podcast episodes (before/after the episode).
This is a helper tool. It never deletes or overwrites the audio.

The script can:
1) "detect" => analyses each file and writes trim_suggestions.csv with a suggested content_start / content_end per episode. You open that CSV and correct anything that looks wrong.
2) "apply" => reads the (reviewed) CSV and writes trimmed copies into a separate folder, leaving originals untouched.

Detection uses two signals:
 - Silence gaps (ffmpeg silencedetect) - finds clean places to cut.
 - Optional transcription (faster-whisper, --whisper) - transcribes the first/last few minutes and flags ad language, then snaps the boundary to the nearest silence so cuts don't land mid-word.

Requirements:
 - ffmpeg + ffprobe on PATH
 - pip install faster-whisper # only if you use --whisper

Usage:
    python podcast_adtrim.py detect ./episodes
    python podcast_adtrim.py detect ./episodes --whisper --model small
    # ...review trim_suggestions.csv, fix any rows...
    python podcast_adtrim.py apply ./episodes --csv trim_suggestions.csv
"""

import argparse
import csv
import os
import re
import subprocess
import sys

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")

# phrases that strongly suggest an ad/sponsor read
AD_PATTERNS = [
    r"sponsor", r"sponsored by", r"brought to you by", r"promo code",
    r"use code", r"discount code", r"dot com slash", r"\.com/", r"slash",
    r"this episode is", r"today's episode", r"patreon", r"support the show",
    r"free trial", r"sign up", r"go to ", r"visit ", r"terms and conditions",
    r"advertis", r"our partners", r"check out",
]
AD_RE = re.compile("|".join(AD_PATTERNS), re.I)

# how many seconds at the start/end to inspect for ads
INTRO_WINDOW = 180
OUTRO_WINDOW = 180

SILENCE_NOISE = "-30dB"   # threshold below which audio counts as "silent"
SILENCE_MINDUR = 0.6      # minimum silence length (seconds) to register


# ffmpeg helpers
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def silences(path):
    """Return list of (silence_start, silence_end) in seconds"""
    err = run(["ffmpeg", "-i", path, "-af", f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MINDUR}", "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    pairs = []
    for i, s in enumerate(starts):
        pairs.append((s, ends[i] if i < len(ends) else None))
    return pairs


def nearest_silence_edge(sil, t, prefer):
    """Snap time t to the closest silence boundary. prefer='end' returns silence_end (good for content START), and prefer='start' returns silence_start (good for content END)"""
    cands = []
    for s, e in sil:
        if prefer == "end" and e is not None:
            cands.append(e)
        if prefer == "start":
            cands.append(s)
    if not cands:
        return t
    return min(cands, key=lambda x: abs(x - t))


# silence-only guess
def silence_guess(sil, dur):
    """Crude fallback when transcription is off: bracket using the first front-window gap and the last back-window gap. Treat as a hint."""
    fw = min(INTRO_WINDOW, dur / 3)
    bw = min(OUTRO_WINDOW, dur / 3)
    front = sorted([(s, e) for s, e in sil if e is not None and s < fw])
    back = sorted([s for s, e in sil if s > dur - bw])
    start = front[0][1] if front else 0.0
    end = back[-1] if back else dur
    return start, end


# transcription
_model = None

def get_model(name):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(name, device="auto", compute_type="int8")
    return _model


def transcribe_window(path, model_name, start, length):
    """Transcribe `length` seconds beginning at `start`. Returns list of (seg_start, seg_end, text) with timestamps relative to the file"""
    model = get_model(model_name)
    clip = path + f".__clip_{int(start)}.wav"
    run(["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", path, "-ar", "16000", "-ac", "1", clip, "-loglevel", "error"])
    try:
        segs, _ = model.transcribe(clip, language=None, vad_filter=True)
        return [(start + s.start, start + s.end, s.text.strip()) for s in segs]
    finally:
        if os.path.exists(clip):
            os.remove(clip)


def whisper_guess(path, model_name, dur, sil):
    """Use transcription to locate where ads stop (intro) / start (outro), then snap to the nearest silence,r eturns (start, end, head, tail)"""
    intro = transcribe_window(path, model_name, 0, min(INTRO_WINDOW, dur))
    back_start = max(0.0, dur - OUTRO_WINDOW)
    outro = transcribe_window(path, model_name, back_start, dur - back_start)

    # content start = end of the LAST ad-matching segment in the intro region
    content_start = 0.0
    for s_start, s_end, text in intro:
        if AD_RE.search(text):
            content_start = max(content_start, s_end)
    # content end = start of the FIRST ad-matching segment in the outro region
    content_end = dur
    for s_start, s_end, text in outro:
        if AD_RE.search(text):
            content_end = min(content_end, s_start)
            break

    content_start = nearest_silence_edge(sil, content_start, prefer="end") \
        if content_start > 0 else 0.0
    content_end = nearest_silence_edge(sil, content_end, prefer="start") \
        if content_end < dur else dur

    head = " | ".join(t for _, _, t in intro[:4])
    tail = " | ".join(t for _, _, t in outro[-4:])
    return content_start, content_end, head[:300], tail[:300]


# commands
def list_audio(folder):
    files = [f for f in sorted(os.listdir(folder))
             if f.lower().endswith(AUDIO_EXTS)]
    return [os.path.join(folder, f) for f in files]


def cmd_detect(args):
    paths = list_audio(args.folder)
    if not paths:
        sys.exit(f"No audio files found in {args.folder}")
    out_csv = args.csv or os.path.join(args.folder, "trim_suggestions.csv")
    rows = []
    for i, path in enumerate(paths, 1):
        name = os.path.basename(path)
        print(f"[{i}/{len(paths)}] {name}")
        dur = duration(path)
        sil = silences(path)
        head = tail = ""
        if args.whisper:
            start, end, head, tail = whisper_guess(path, args.model, dur, sil)
            method = "whisper+silence"
        else:
            start, end = silence_guess(sil, dur)
            method = "silence-only(hint)"
        print(f"    duration {dur:.1f}s -> suggest keep {start:.1f}..{end:.1f}  [{method}]")
        rows.append({
            "file": name,
            "duration_s": f"{dur:.2f}",
            "content_start_s": f"{start:.2f}",
            "content_end_s": f"{end:.2f}",
            "method": method,
            "intro_transcript": head,
            "outro_transcript": tail,
        })
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {out_csv}")
    print("Review/edit content_start_s and content_end_s, then run `apply`.")


def cmd_apply(args):
    csv_path = args.csv or os.path.join(args.folder, "trim_suggestions.csv")
    if not os.path.exists(csv_path):
        sys.exit(f"CSV not found: {csv_path}. Run `detect` first.")
    outdir = args.outdir or os.path.join(args.folder, "trimmed")
    os.makedirs(outdir, exist_ok=True)
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        src = os.path.join(args.folder, row["file"])
        if not os.path.exists(src):
            print(f"  missing, skip: {row['file']}")
            continue
        start = float(row["content_start_s"])
        end = float(row["content_end_s"])
        dst = os.path.join(outdir, row["file"])
        cmd = ["ffmpeg", "-y", "-ss", f"{start}", "-to", f"{end}", "-i", src, "-c", "copy", dst, "-loglevel", "error"]
        r = run(cmd)
        if r.returncode != 0 or not os.path.exists(dst):
            # stream-copy can fail on some containers, re-encode as fallback
            run(["ffmpeg", "-y", "-ss", f"{start}", "-to", f"{end}", "-i", src, dst, "-loglevel", "error"])
        kept = end - start
        print(f"  {row['file']}: kept {kept:.1f}s -> {dst}")
    print(f"\nTrimmed copies in {outdir}. Originals untouched.")


def main():
    ap = argparse.ArgumentParser(description="Detect & trim podcast intro/outro ads.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="analyse files and write a suggestions CSV")
    d.add_argument("folder")
    d.add_argument("--whisper", action="store_true", help="use transcription (more accurate)")
    d.add_argument("--model", default="small", help="whisper model: tiny/base/small/medium")
    d.add_argument("--csv", help="output CSV path")
    d.set_defaults(func=cmd_detect)

    a = sub.add_parser("apply", help="trim files using the reviewed CSV")
    a.add_argument("folder")
    a.add_argument("--csv", help="input CSV path")
    a.add_argument("--outdir", help="where to write trimmed copies")
    a.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
