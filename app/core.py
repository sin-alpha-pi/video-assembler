"""
Dialogue Assembler core: finds each script line in the speaker clips (faster-whisper),
cuts it out, and assembles everything in script order with slide-left transitions.
Used by gui.py (window app) and runnable headless for testing:

    python core.py <folder>                 analyze + render
    python core.py <folder> --dry-run       analyze only
"""

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
TRANSITION_NAMES = {"transition.mp3", "whoosh.mp3"}
SEARCH_BEFORE = 0.5
SEARCH_AFTER = 0.8


@dataclass
class Settings:
    transition: float = 0.5      # slide duration (s)
    whoosh_volume: float = 1.0
    mp3_offset: float = 0.3      # default start of an extra mp3 after its clip starts
    mp3_volume: float = 1.0
    min_match: float = 0.70
    padding: float = 0.15        # room kept before first / after last word
    silence_db: float = -35
    crf: int = 18


# ------------------------------------------------------------------ paths / processes
def base_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def ffmpeg_bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = base_dir() / "ffmpeg" / exe
    return str(bundled) if bundled.exists() else name


def model_path():
    bundled = base_dir() / "models" / "small.en"
    return str(bundled) if bundled.exists() else "small.en"


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{r.stderr[-1500:]}")
    return r.stderr


def run_with_progress(cmd, total, progress):
    p = subprocess.Popen([str(c) for c in cmd] + ["-progress", "pipe:1", "-nostats"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    err = []
    t = threading.Thread(target=lambda: err.extend(p.stderr.readlines()), daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us=") and total > 0:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    p.wait()
    t.join()
    if p.returncode != 0:
        raise RuntimeError("FFmpeg failed:\n" + "".join(err)[-1500:])


def probe(path):
    out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW, encoding="utf-8", errors="replace").stdout
    info = json.loads(out or "{}")
    if "format" not in info:
        raise RuntimeError(f"Can't read {Path(path).name}")
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    fps, w, h, rot = None, None, None, 0
    if v:
        w, h = v["width"], v["height"]
        if v.get("avg_frame_rate", "0/0") != "0/0":
            n, d = v["avg_frame_rate"].split("/")
            fps = round(float(n) / float(d), 3) if float(d) else None
        rot = int(float(v.get("tags", {}).get("rotate", 0)))
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"duration": float(info["format"]["duration"]), "width": w, "height": h, "fps": fps,
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


# ------------------------------------------------------------------ text
def norm(word):
    return re.sub(r"[^a-z0-9']", "", word.lower().replace("’", "'"))


def norm_words(text):
    return [n for n in (norm(w) for w in re.split(r"[\s\-—–]+", text)) if n]


def parse_script(path, mp3_offset):
    items, pending_left = [], False
    for raw in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
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
            items.append({"type": "clip", "file": parts[0], "mp3": parts[1] if len(parts) > 1 else None,
                          "offset": float(off.group(1)) if off else mp3_offset, "left": pending_left})
            pending_left = False
        m = re.match(r"^([A-Za-z][\w .'-]{0,30}):\s*(.+)$", line)
        if m:
            items.append({"type": "line", "speaker": m.group(1).strip(),
                          "text": m.group(2).strip(), "left": pending_left})
            pending_left = False
    return items


# ------------------------------------------------------------------ whisper
_model = None
_model_lock = threading.Lock()


def get_model(log):
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            log("Loading speech recognition model...")
            _model = WhisperModel(model_path(), device="cpu", compute_type="int8",
                                  cpu_threads=os.cpu_count() or 4)
    return _model


def decode_audio(path):
    """16 kHz mono float32 audio via the bundled FFmpeg (avoids PyAV version issues)."""
    import numpy as np
    r = subprocess.run([ffmpeg_bin("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                       capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"Can't read audio of {Path(path).name}:\n"
                           + r.stderr.decode("utf-8", "replace")[-800:])
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe(path, cache_dir, log, progress=None):
    cache = cache_dir / (path.name + ".json")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    model = get_model(log)
    audio = decode_audio(path)
    segments, info = model.transcribe(audio, language="en", word_timestamps=True,
                                      condition_on_previous_text=False, beam_size=5)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)})
        if progress and info.duration:
            progress(min(1.0, seg.end / info.duration))
    cache_dir.mkdir(exist_ok=True)
    cache.write_text(json.dumps(words), encoding="utf-8")
    return words


# ------------------------------------------------------------------ matching
def find_line(text, clips, used):
    target_words = norm_words(text)
    target = " ".join(target_words)
    n = len(target_words)
    rough = []
    for ci, c in enumerate(clips):
        toks = c["toks"]
        for i in range(0, max(1, len(toks) - n + 1)):
            rough.append((difflib.SequenceMatcher(None, target, " ".join(toks[i:i + n])).ratio(), ci, i))
    rough.sort(reverse=True)
    best, seen = [], set()
    for _, ci, i0 in rough[:15]:
        toks = clips[ci]["toks"]
        for i in range(max(0, i0 - 3), min(i0 + 4, len(toks))):
            for size in range(max(1, int(n * 0.6)), n + 5):
                j = min(i + size, len(toks))
                if j <= i or (ci, i, j) in seen:
                    continue
                seen.add((ci, i, j))
                if any(u_ci == ci and i < u_j and j > u_i for u_ci, u_i, u_j in used):
                    continue
                r = difflib.SequenceMatcher(None, target, " ".join(toks[i:j])).ratio()
                best.append((r, ci, i, j))
    if not best:
        return None, None
    best.sort(key=lambda b: (-b[0], b[3] - b[2]))
    top = best[0]
    second = next((b for b in best[1:] if b[1] != top[1] or b[3] <= top[2] or b[2] >= top[3]), None)
    return top, second


def refine_edges(path, w_start, w_end, lo, hi, s):
    a = max(lo, w_start - SEARCH_BEFORE)
    b = min(hi, w_end + SEARCH_AFTER)
    log = run([ffmpeg_bin("ffmpeg"), "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
               "-i", path, "-vn", "-af", f"silencedetect=noise={s.silence_db}dB:d=0.08", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    sil = [(st, ends[k] if k < len(ends) else b - a) for k, st in enumerate(starts)]
    sp_start, sp_end = a, b
    if sil and sil[0][0] <= 0.03:
        sp_start = a + sil[0][1]
    if sil and sil[-1][1] >= (b - a) - 0.03:
        sp_end = a + sil[-1][0]
    sp_start = min(sp_start, w_start + 0.3)
    sp_end = max(sp_end, w_end - 0.3)
    return max(lo, sp_start - s.padding), min(hi, sp_end + s.padding)


# ------------------------------------------------------------------ analyze
class Plan:
    def __init__(self):
        self.segments, self.rows, self.whoosh, self.problems = [], [], None, 0

    @property
    def ok(self):
        return self.problems == 0 and bool(self.segments)

    def text(self):
        out = []
        for r in self.rows:
            out.append(f"{r['n']:>3} {'>' if r['left'] else ' '} {r['label']}\n      {r['src']}")
            out += [f"      {t}" for _, t in r["notes"]]
        return "\n".join(out) + "\n"


def analyze(folder, s, log=print, progress=lambda r: None):
    folder = Path(folder).resolve()
    txts = [f for f in folder.glob("*.txt") if f.name != "cut_list.txt"]
    if len(txts) != 1:
        raise RuntimeError("The folder needs exactly one .txt script.")
    items = parse_script(txts[0], s.mp3_offset)
    if not items:
        raise RuntimeError("No dialogue lines or [clip] tags found in the script.")
    files = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
    tagged = {it["file"].lower() for it in items if it["type"] == "clip"}
    videos = sorted(f for f in files.values() if f.suffix.lower() in VIDEO_EXT
                    and f.name.lower() not in tagged and not f.stem.endswith("_assembled"))

    speakers = list(dict.fromkeys(it["speaker"] for it in items if it["type"] == "line"))
    by_speaker = {sp: [] for sp in speakers}
    for f in videos:
        hits = [sp for sp in speakers
                if re.search(rf"(?<![a-z]){re.escape(sp.lower())}(?![a-z])", f.stem.lower())]
        if len(hits) == 1:
            by_speaker[hits[0]].append(f)
        else:
            log(f"! {f.name}: {'no' if not hits else 'several'} speaker names in filename, ignored")
    for sp, fs in by_speaker.items():
        if not fs:
            raise RuntimeError(f"No clips found for speaker '{sp}' (the name must be in the filename).")

    cache_dir = folder / "_transcripts"
    all_clips = [f for fs in by_speaker.values() for f in fs]
    clip_data, done = {}, 0
    for sp, fs in by_speaker.items():
        clip_data[sp] = []
        for f in fs:
            cached = (cache_dir / (f.name + ".json")).exists()
            log(f"{'Using saved transcript for' if cached else 'Transcribing'} {f.name}"
                f" ({done + 1}/{len(all_clips)})")
            words = transcribe(f, cache_dir, log,
                               lambda r, d=done: progress((d + r) / len(all_clips)))
            words = [w for w in words if norm(w["w"])]
            clip_data[sp].append({"path": f, "words": words, "toks": [norm(w["w"]) for w in words],
                                  "info": probe(f)})
            done += 1
            progress(done / len(all_clips))

    plan, used = Plan(), {}
    for n, it in enumerate(items, 1):
        if it["type"] == "clip":
            p = files.get(it["file"].lower())
            if not p:
                raise RuntimeError(f"Clip named in the script not found: {it['file']}")
            info = probe(p)
            seg = {"path": p, "start": 0.0, "end": info["duration"], "mute": True,
                   "has_audio": info["has_audio"], "left": it["left"], "mp3": None, "offset": it["offset"]}
            row = {"n": n, "left": it["left"], "label": f"[{p.name}] muted",
                   "src": f"0.00-{info['duration']:.2f}", "level": "ok", "notes": []}
            if it["mp3"]:
                mp3 = files.get(it["mp3"].lower())
                if not mp3:
                    raise RuntimeError(f"mp3 named in the script not found: {it['mp3']}")
                seg["mp3"] = mp3
                row["label"] += f" + {mp3.name} at {it['offset']}s"
                over = it["offset"] + probe(mp3)["duration"] - info["duration"]
                if over > 0.01:
                    row["notes"].append(("warn", f"mp3 runs {over:.2f}s past the clip end and will be cut off"))
            plan.segments.append(seg)
            plan.rows.append(row)
            continue

        clips = clip_data[it["speaker"]]
        top, second = find_line(it["text"], clips, used.setdefault(it["speaker"], []))
        row = {"n": n, "left": it["left"], "label": f"{it['speaker']}: {it['text']}",
               "src": "", "level": "ok", "notes": []}
        if not top or top[0] < s.min_match:
            plan.problems += 1
            row["level"] = "bad"
            row["src"] = f"NOT FOUND (best match {top[0] * 100 if top else 0:.0f}%)"
            if top:
                heard = " ".join(x["w"] for x in clips[top[1]]["words"][top[2]:top[3]])
                row["notes"].append(("bad", f'closest in {clips[top[1]]["path"].name}: "{heard}"'))
            plan.rows.append(row)
            continue
        score, ci, i, j = top
        c = clips[ci]
        used[it["speaker"]].append((ci, i, j))
        w = c["words"]
        lo = w[i - 1]["e"] + 0.02 if i > 0 else 0.0
        hi = w[j]["s"] - 0.02 if j < len(w) else c["info"]["duration"]
        start, end = refine_edges(c["path"], w[i]["s"], w[j - 1]["e"], lo, hi, s)
        plan.segments.append({"path": c["path"], "start": start, "end": end, "mute": False,
                              "has_audio": c["info"]["has_audio"], "left": it["left"], "mp3": None})
        row["src"] = f"{c['path'].name}  {start:.2f}-{end:.2f}  match {score * 100:.0f}%"
        if score < 0.85:
            row["level"] = "warn"
            row["notes"].append(("warn", 'weak match, heard: "' + " ".join(x["w"] for x in w[i:j]) + '"'))
        if second and second[0] >= 0.85:
            row["notes"].append(("warn", "line also appears elsewhere, used the best match"))
        plan.rows.append(row)

    plan.whoosh = next((f for f in files.values() if f.name.lower() in TRANSITION_NAMES), None)
    if not plan.whoosh:
        log("! no transition.mp3 found, slides will be silent")
    (folder / "cut_list.txt").write_text(plan.text(), encoding="utf-8")
    return plan


# ------------------------------------------------------------------ render
def _prepare(seg, idx, tmp, W, H, FPS, s):
    start, end = seg["start"], seg["end"]
    dur = end - start
    out = tmp / f"seg_{idx:03d}.mp4"
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p,settb=AVTB")
    aform = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    cmd = [ffmpeg_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", seg["path"]]
    if seg.get("mp3"):
        cmd += ["-i", seg["mp3"]]
        af = (f"[1:a]{aform},volume={s.mp3_volume},adelay={int(seg['offset'] * 1000)}:all=1,"
              f"apad,atrim=0:{dur:.3f}[a]")
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


def render(plan, output, s, log=print, progress=lambda r: None):
    if not plan.ok:
        raise RuntimeError("Fix the cut list problems first.")
    first = probe(plan.segments[0]["path"])
    W, H = (first["width"] or 1080), (first["height"] or 1920)
    W, H = W - W % 2, H - H % 2
    FPS = first["fps"] or 30
    plan.segments[0]["left"] = False
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        prepared = []
        for k, seg in enumerate(plan.segments):
            log(f"Preparing segment {k + 1}/{len(plan.segments)}")
            p, d = _prepare(seg, k, tmp, W, H, FPS, s)
            prepared.append((p, d, seg["left"]))
            progress(0.5 * (k + 1) / len(plan.segments))

        d = s.transition
        inputs, parts = [], []
        for i, (p, _, _) in enumerate(prepared):
            inputs += ["-i", p]
            parts.append(f"[{i}:v]settb=AVTB[in{i}]")
        v, a = "[in0]", "[0:a]"
        total = prepared[0][1]
        centers = []
        for i in range(1, len(prepared)):
            _, dur, left = prepared[i]
            vo, ao = f"[v{i}]", f"[a{i}]"
            if left:
                dd = min(d, total, dur) - 0.01
                parts.append(f"{v}[in{i}]xfade=transition=slideleft:duration={dd:.3f}:offset={total - dd:.3f}{vo}")
                parts.append(f"{a}[{i}:a]acrossfade=d={dd:.3f}{ao}")
                centers.append(total - dd / 2)
                total += dur - dd
            else:
                parts.append(f"{v}{a}[in{i}][{i}:a]concat=n=2:v=1:a=1{vo}{ao}")
                total += dur
            v, a = vo, ao
        if plan.whoosh and centers:
            wlen = probe(plan.whoosh)["duration"]
            inputs += ["-i", plan.whoosh]
            wi, n = len(prepared), len(centers)
            parts.append(f"[{wi}:a]aresample=48000,aformat=channel_layouts=stereo,volume={s.whoosh_volume},"
                         f"asplit={n}" + "".join(f"[w{k}]" for k in range(n)))
            for k, c in enumerate(centers):
                parts.append(f"[w{k}]adelay={int(max(0.0, c - wlen / 2) * 1000)}:all=1[wd{k}]")
            parts.append(f"{a}" + "".join(f"[wd{k}]" for k in range(n)) +
                         f"amix=inputs={n + 1}:duration=first:normalize=0[aout]")
            a = "[aout]"
        log("Assembling final video...")
        run_with_progress([ffmpeg_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", *inputs,
                           "-filter_complex", ";".join(parts), "-map", v, "-map", a,
                           "-c:v", "libx264", "-preset", "medium", "-crf", str(s.crf),
                           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output],
                          total, lambda r: progress(0.5 + 0.5 * r))
    progress(1.0)
    return total, len(centers)


def output_path(folder):
    folder = Path(folder).resolve()
    return folder / f"{folder.name}_assembled.mp4"


if __name__ == "__main__":
    folder = sys.argv[1]
    s = Settings()
    plan = analyze(folder, s)
    print(plan.text())
    if "--dry-run" not in sys.argv:
        print(render(plan, output_path(folder), s))
