#!/usr/bin/env python3
"""
Dialogue assembler: cuts speaker clips into single lines using the video script,
puts them in script order, adds slide-left transitions + whoosh and extra audio.

FOLDER CONTENTS
    script.txt                  the video script (any .txt name works if it's the only one)
    mentor_1.mp4, mentor_2.mp4  speaker clips; the speaker name must be in the filename
    husband_1.mp4, ...
    intro.mp4, background.mp4   non-dialogue clips (named in the script, see below)
    background.mp3              extra audio
    transition.mp3              whoosh used for every [left] transition

SCRIPT FORMAT
    - Dialogue lines look like   Name: text     (Name must appear in clip filenames)
    - Everything else (titles, headers, descriptions) is ignored.
    - Tags in square brackets, on their own line or at the start of a line:
        [left]                               slide-left + whoosh into the next item
        [intro.mp4]                          insert a whole clip, muted
        [background.mp4 + background.mp3]    insert a whole clip, muted, with the mp3
        [background.mp4 + background.mp3 @1.2]   mp3 starts 1.2 s into the clip
                                             (without @ it starts MP3_OFFSET seconds in)
    Example:
        [intro.mp4]
        [left]
        [background.mp4 + background.mp3]
        [left]
        Mentor: So, what happened with the doctor?
        Husband: You were right. ...

USAGE
    python dialogue_assemble.py /path/to/folder
    python dialogue_assemble.py /path/to/folder --dry-run   (match + show cut list only)
    python dialogue_assemble.py /path/to/folder -o final.mp4

Transcripts are cached in <folder>/_transcripts, so re-runs are fast.
Delete that folder if you replace a clip.

Requires: Python 3.8+, FFmpeg, openai-whisper (pip install openai-whisper).
"""

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ============================ SETTINGS ======================================
WHISPER_MODEL       = "small.en"  # tiny.en / base.en / small.en / medium.en (slower, more accurate)
TRANSITION_DURATION = 0.5         # seconds the slide takes
WHOOSH_VOLUME       = 1.0
MP3_OFFSET          = 0.3         # default start of an extra mp3 after its clip starts
MP3_VOLUME          = 1.0

MIN_MATCH           = 0.70        # below this similarity a script line counts as "not found"

# Cut points around each line
KEEP_PADDING         = 0.15       # seconds of room kept before the first / after the last word
SEARCH_BEFORE        = 0.5        # how far before Whisper's first word to look for the real start
SEARCH_AFTER         = 0.8        # how far after Whisper's last word to look for the real end
SILENCE_THRESHOLD_DB = -35        # quieter than this counts as silence (-30 if noisy, -40 if quiet)

# Output format. None = take it from the first clip used.
TARGET_WIDTH  = None
TARGET_HEIGHT = None
TARGET_FPS    = None
VIDEO_CRF     = 18
# ============================================================================

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
TRANSITION_NAMES = {"transition.mp3", "whoosh.mp3"}
_model = None


# ---------------------------------------------------------------- helpers ---
def run(cmd, capture=False):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"\nFFmpeg failed:\n{' '.join(map(str, cmd))}\n\n{r.stderr[-2000:]}")
    return r.stderr if capture else None


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    fps, w, h, rot = None, None, None, 0
    if v:
        w, h = v["width"], v["height"]
        if v.get("avg_frame_rate", "0/0") != "0/0":
            n, d = v["avg_frame_rate"].split("/")
            fps = round(float(n) / float(d), 3)
        rot = int(v.get("tags", {}).get("rotate", 0))
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"duration": float(info["format"]["duration"]), "width": w, "height": h, "fps": fps,
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def norm(word):
    return re.sub(r"[^a-z0-9']", "", word.lower().replace("’", "'"))


def norm_words(text):
    return [n for n in (norm(w) for w in re.split(r"[\s\-—]+", text)) if n]


# ---------------------------------------------------------------- script ----
def parse_script(path):
    items, pending_left, current = [], False, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        while True:
            m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
            if not m:
                break
            tag, line = m.group(1).strip(), m.group(2).strip()
            if tag.lower() == "left":
                pending_left = True
                continue
            off = re.search(r"@\s*([\d.]+)\s*$", tag)
            if off:
                tag = tag[:off.start()].strip()
            parts = [p.strip() for p in tag.split("+")]
            items.append({"type": "clip", "file": parts[0],
                          "mp3": parts[1] if len(parts) > 1 else None,
                          "offset": float(off.group(1)) if off else MP3_OFFSET,
                          "left": pending_left})
            pending_left = False
            current = None                      # a clip ends the speaker's block
        if not line:
            continue                            # blank lines keep the current speaker
        # "Name: text"  or  "Name:" on its own line followed by the text lines
        m = re.match(r"^([A-Za-z][\w'-]*(?: [\w'-]+){0,2}):\s*(.*)$", line)  # name = max 3 words
        if m:
            current, line = m.group(1).strip().capitalize(), m.group(2).strip()  # "mentor" = "Mentor"
            if not line:
                continue
        elif current and not re.search("[.?!:;,\u2026\"'\u201d\u2019)]$", line):
            current = None                      # heading (no end punctuation) ends the block
            continue
        if current:
            items.append({"type": "line", "speaker": current, "text": line, "left": pending_left})
            pending_left = False
    return items


# ---------------------------------------------------------------- whisper ---
def transcribe(path, cache_dir):
    cache = cache_dir / (path.name + ".json")
    if cache.exists():
        return json.loads(cache.read_text())
    global _model
    import whisper
    if _model is None:
        print(f"  loading Whisper model '{WHISPER_MODEL}'...")
        _model = whisper.load_model(WHISPER_MODEL)
    print(f"  transcribing {path.name}...")
    r = _model.transcribe(str(path), language="en", word_timestamps=True,
                          condition_on_previous_text=False, fp16=False)
    words = [{"w": w["word"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3)}
             for seg in r["segments"] for w in seg.get("words", [])]
    cache_dir.mkdir(exist_ok=True)
    cache.write_text(json.dumps(words, indent=0))
    return words


# ---------------------------------------------------------------- matching --
def find_line(text, clips, used):
    """Find the best matching word span for a script line across a speaker's clips."""
    target_words = norm_words(text)
    target = " ".join(target_words)
    n = len(target_words)
    rough = []
    for ci, c in enumerate(clips):
        toks = c["toks"]
        for i in range(0, max(1, len(toks) - n + 1)):
            cand = " ".join(toks[i:i + n])
            rough.append((difflib.SequenceMatcher(None, target, cand).ratio(), ci, i))
    rough.sort(reverse=True)
    best = []
    for _, ci, i0 in rough[:15]:              # refine the best rough spots
        toks = clips[ci]["toks"]
        for i in range(max(0, i0 - 3), i0 + 4):
            for size in range(max(1, int(n * 0.6)), n + 5):   # allow skipped/extra words
                j = min(i + size, len(toks))
                if j <= i:
                    break
                if any(u_ci == ci and i < u_j and j > u_i for u_ci, u_i, u_j in used):
                    continue
                r = difflib.SequenceMatcher(None, target, " ".join(toks[i:j])).ratio()
                best.append((r, ci, i, j))
    if not best:
        return None, None
    best.sort(reverse=True)
    top = best[0]
    second = next((b for b in best[1:] if b[1] != top[1] or b[3] <= top[2] or b[2] >= top[3]), None)
    return top, second


def refine_edges(path, w_start, w_end, lo, hi):
    """Use the audio to place cuts just before the first and after the last sound."""
    a = max(lo, w_start - SEARCH_BEFORE)
    b = min(hi, w_end + SEARCH_AFTER)
    log = run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
               "-i", path, "-vn", "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d=0.08",
               "-f", "null", "-"], capture=True)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    sil = [(s, ends[k] if k < len(ends) else b - a) for k, s in enumerate(starts)]
    sp_start, sp_end = a, b
    if sil and sil[0][0] <= 0.03:
        sp_start = a + sil[0][1]
    if sil and sil[-1][1] >= (b - a) - 0.03:
        sp_end = a + sil[-1][0]
    sp_start = min(sp_start, w_start + 0.3)   # don't trust silence that eats into words
    sp_end = max(sp_end, w_end - 0.3)
    return max(lo, sp_start - KEEP_PADDING), min(hi, sp_end + KEEP_PADDING)


# ---------------------------------------------------------------- render ----
def prepare_segment(seg, idx, tmp, W, H, FPS):
    start, end = seg["start"], seg["end"]
    dur = end - start
    out = tmp / f"seg_{idx:03d}.mp4"
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p,settb=AVTB")
    aform = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", seg["path"]]
    if seg.get("mp3"):
        ms = int(seg["offset"] * 1000)
        cmd += ["-i", seg["mp3"]]
        af = f"[1:a]{aform},volume={MP3_VOLUME},adelay={ms}:all=1,apad,atrim=0:{dur:.3f}[a]"
    elif seg["mute"] or not seg["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        af = f"[1:a]{aform}[a]"
    else:
        af = f"[0:a]{aform},apad,atrim=0:{dur:.3f}[a]"
    cmd += ["-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "fast", "-crf", "14",
            "-c:a", "aac", "-b:a", "320k", out]
    run(cmd)
    return out, probe(out)["duration"]


def assemble(segments, whoosh, output):
    """segments: list of (path, duration, left_flag)."""
    d = TRANSITION_DURATION
    inputs, parts = [], []
    for i, (p, _, _) in enumerate(segments):
        inputs += ["-i", p]
        parts.append(f"[{i}:v]settb=AVTB[in{i}]")
    v, a = "[in0]", "[0:a]"
    total = segments[0][1]
    centers = []
    for i in range(1, len(segments)):
        _, dur, left = segments[i]
        vo, ao = f"[v{i}]", f"[a{i}]"
        if left:
            dd = min(d, total, dur) - 0.01
            parts.append(f"{v}[in{i}]xfade=transition=slideleft:duration={dd:.3f}:"
                         f"offset={total - dd:.3f}{vo}")
            parts.append(f"{a}[{i}:a]acrossfade=d={dd:.3f}{ao}")
            centers.append(total - dd / 2)
            total += dur - dd
        else:
            parts.append(f"{v}{a}[in{i}][{i}:a]concat=n=2:v=1:a=1{vo}{ao}")
            total += dur
        v, a = vo, ao
    if whoosh and centers:
        wlen = probe(whoosh)["duration"]
        inputs += ["-i", whoosh]
        wi, n = len(segments), len(centers)
        parts.append(f"[{wi}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={WHOOSH_VOLUME},asplit={n}" + "".join(f"[w{k}]" for k in range(n)))
        for k, c in enumerate(centers):
            parts.append(f"[w{k}]adelay={int(max(0.0, c - wlen / 2) * 1000)}:all=1[wd{k}]")
        parts.append(f"{a}" + "".join(f"[wd{k}]" for k in range(n)) +
                     f"amix=inputs={n + 1}:duration=first:normalize=0[aout]")
        a = "[aout]"
    elif centers:
        print("  ! no transition.mp3 found, transitions will be silent")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(parts), "-map", v, "-map", a,
         "-c:v", "libx264", "-preset", "medium", "-crf", str(VIDEO_CRF),
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output])
    return total, centers


# ---------------------------------------------------------------- main ------
def main():
    ap = argparse.ArgumentParser(description="Cut and assemble dialogue clips from a script.")
    ap.add_argument("folder", type=Path)
    ap.add_argument("-s", "--script", type=Path, help="script file (default: the .txt in the folder)")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="show the cut list, render nothing")
    args = ap.parse_args()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("FFmpeg not found. Install it and make sure 'ffmpeg' works in a terminal.")

    folder = args.folder.resolve()
    output = (args.output or folder / f"{folder.name}_assembled.mp4").resolve()
    script = args.script
    if not script:
        txts = [f for f in folder.glob("*.txt") if f.name != "cut_list.txt"]
        if len(txts) != 1:
            sys.exit("Put exactly one .txt script in the folder, or pass it with --script.")
        script = txts[0]
    items = parse_script(script)
    if not items:
        sys.exit("No dialogue lines or [clip] tags found in the script.")

    whoosh = next((f for f in folder.iterdir() if f.name.lower() in TRANSITION_NAMES), None)
    tagged = {it["file"].lower() for it in items if it["type"] == "clip"}
    videos = [f for f in sorted(folder.iterdir()) if f.suffix.lower() in VIDEO_EXT
              and f.name.lower() not in tagged and f.resolve() != output]

    # assign clips to speakers
    speakers = sorted({it["speaker"] for it in items if it["type"] == "line"}, key=str.lower)
    by_speaker = {s: [] for s in speakers}
    for f in videos:
        hits = [s for s in speakers
                if re.search(rf"(?<![a-z]){re.escape(s.lower())}(?![a-z])", f.stem.lower())]
        if len(hits) == 1:
            by_speaker[hits[0]].append(f)
        else:
            print(f"  ! {f.name}: {'no' if not hits else 'several'} speaker names in filename, ignored")
    for s, fs in by_speaker.items():
        if not fs:
            sys.exit(f"No clips found for speaker '{s}' (the name must be in the filename).")

    # transcribe
    cache_dir = folder / "_transcripts"
    clip_data = {}
    print("Transcripts:")
    for s, fs in by_speaker.items():
        clip_data[s] = []
        for f in fs:
            words = [w for w in transcribe(f, cache_dir) if norm(w["w"])]
            clip_data[s].append({"path": f, "words": words, "toks": [norm(w["w"]) for w in words],
                                 "info": probe(f)})
        print(f"  {s}: {len(fs)} clip(s)")

    # build the cut list
    segments, used, problems = [], {}, 0
    report = []
    for n, it in enumerate(items, 1):
        if it["type"] == "clip":
            p = folder / it["file"]
            if not p.exists():
                sys.exit(f"Clip named in script not found: {it['file']}")
            info = probe(p)
            seg = {"path": p, "start": 0.0, "end": info["duration"], "mute": True,
                   "has_audio": info["has_audio"], "left": it["left"], "mp3": None,
                   "offset": it["offset"]}
            desc = f"[{p.name}] muted"
            if it["mp3"]:
                mp3 = folder / it["mp3"]
                if not mp3.exists():
                    sys.exit(f"mp3 named in script not found: {it['mp3']}")
                seg["mp3"] = mp3
                desc += f" + {mp3.name} at {it['offset']}s"
                over = it["offset"] + probe(mp3)["duration"] - info["duration"]
                if over > 0.01:
                    desc += f"  ! mp3 runs {over:.2f}s past clip end, cut off"
            segments.append(seg)
            report.append(f"{n:>3} {'>' if it['left'] else ' '} {desc}  (0.00-{info['duration']:.2f})")
            continue

        clips = clip_data[it["speaker"]]
        top, second = find_line(it["text"], clips, used.setdefault(it["speaker"], []))
        short = (it["text"][:55] + "...") if len(it["text"]) > 58 else it["text"]
        if not top or top[0] < MIN_MATCH:
            problems += 1
            heard = ""
            if top:
                heard = (f"\n      closest: {clips[top[1]]['path'].name}: \""
                         + " ".join(x["w"] for x in clips[top[1]]["words"][top[2]:top[3]]) + "\"")
            report.append(f"{n:>3}   {it['speaker']}: {short}\n      !! NOT FOUND "
                          f"(best match {top[0] * 100 if top else 0:.0f}%){heard}")
            continue
        score, ci, i, j = top
        c = clips[ci]
        used[it["speaker"]].append((ci, i, j))
        w = c["words"]
        lo = w[i - 1]["e"] + 0.02 if i > 0 else 0.0
        hi = w[j]["s"] - 0.02 if j < len(w) else c["info"]["duration"]
        start, end = refine_edges(c["path"], w[i]["s"], w[j - 1]["e"], lo, hi)
        segments.append({"path": c["path"], "start": start, "end": end, "mute": False,
                         "has_audio": c["info"]["has_audio"], "left": it["left"], "mp3": None})
        flag = ""
        if score < 0.85:
            flag += f"\n      ? weak match, heard: \"{' '.join(x['w'] for x in w[i:j])}\""
        if second and second[0] >= 0.85:
            flag += "\n      ? line also appears elsewhere, used the best match"
        report.append(f"{n:>3} {'>' if it['left'] else ' '} {it['speaker']}: {short}\n"
                      f"      {c['path'].name} {start:.2f}-{end:.2f}  match {score * 100:.0f}%{flag}")

    print("\nCut list  ('>' = slide-left in):")
    print("\n".join(report))
    (folder / "cut_list.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    if problems:
        print(f"\n{problems} line(s) not found. Check the cut list; nothing rendered.")
        sys.exit(1)
    if args.dry_run:
        return

    first = probe(segments[0]["path"])
    W, H = TARGET_WIDTH or first["width"], TARGET_HEIGHT or first["height"]
    W, H = W - W % 2, H - H % 2
    FPS = TARGET_FPS or first["fps"] or 30
    if segments[0]["left"]:
        segments[0]["left"] = False
    print(f"\nRendering {len(segments)} segments at {W}x{H} @ {FPS} fps...")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        prepared = []
        for k, seg in enumerate(segments):
            p, d = prepare_segment(seg, k, tmp, W, H, FPS)
            prepared.append((p, d, seg["left"]))
        total, centers = assemble(prepared, whoosh, output)
    print(f"Done: {output}  ({total:.2f}s, {len(centers)} transitions)")


if __name__ == "__main__":
    main()
