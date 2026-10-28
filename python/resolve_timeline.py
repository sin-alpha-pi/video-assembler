#!/usr/bin/env python3
"""
Builds a DaVinci Resolve timeline (FCPXML) from the same folder + script as dialogue_assemble.py.

    python resolve_timeline.py /path/to/folder

Writes <folder>/<foldername>_timeline.fcpxml. In Resolve: File > Import > Timeline...,
pick the file, leave "Automatically import source clips into media pool" ticked.

Timeline layout
    Video 1 / Audio 1   all clips in script order, tight cuts. The clips reference the full
                        original files, so you can drag every edge out in Resolve.
    Audio 2             extra mp3s (e.g. background.mp3) at their offset
    Audio 3             whoosh, centered on every [left] cut
    Transitions         a cross dissolve on every [left] cut plus a marker "Slide left",
                        so you can swap in Resolve's Slide transition there.
    Muted clips         intro / cards / mid cards are set to -96 dB.

Needs dialogue_assemble.py in the same folder (it does the transcribing and line matching,
including the saved transcripts in _transcripts).
"""

import shutil
import sys
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dialogue_assemble as da  # noqa: E402

TRANSITION_DURATION = da.TRANSITION_DURATION
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"


def frame_rate(fps):
    """Return (numerator, denominator) of one frame's duration."""
    for nominal, den in ((23.976, 24000), (29.97, 30000), (59.94, 60000)):
        if abs(fps - nominal) < 0.01:
            return 1001, den
    return 1, int(round(fps))


class Timebase:
    def __init__(self, fps):
        self.num, self.den = frame_rate(fps)
        self.fps = Fraction(self.den, self.num)

    def frames(self, seconds):
        return int(round(Fraction(seconds).limit_denominator(100000) * self.fps))

    def t(self, frames):
        """Frames -> FCPXML rational time string."""
        v = Fraction(frames * self.num, self.den)
        return f"{v.numerator}/{v.denominator}s" if v.denominator != 1 else f"{v.numerator}s"


def build(folder):
    folder = Path(folder).resolve()
    segments, whoosh = da.build_plan(folder)
    first = da.probe(segments[0]["path"])
    tb = Timebase(da.TARGET_FPS or first["fps"] or 30)
    W = da.TARGET_WIDTH or first["width"] or 1920
    H = da.TARGET_HEIGHT or first["height"] or 1080
    segments[0]["left"] = False

    # ---- resources
    res, formats, assets = [], {}, {}
    res.append(f'<format id="r0" frameDuration="{tb.t(1)}" width="{W}" height="{H}"/>')
    res.append(f'<effect id="rx" name="Cross Dissolve" uid="{CROSS_DISSOLVE_UID}"/>')

    def fmt_for(info):
        key = (info["width"], info["height"])
        if key == (W, H):
            return "r0"
        if key not in formats:
            fid = f"r{len(formats) + 1}"
            formats[key] = fid
            res.append(f'<format id="{fid}" frameDuration="{tb.t(1)}" width="{key[0]}" height="{key[1]}"/>')
        return formats[key]

    def asset(path):
        path = Path(path)
        if path in assets:
            return assets[path]
        info = da.probe(path)
        aid = f"a{len(assets) + 1}"
        dur = tb.t(tb.frames(info["duration"]))
        video = info["width"] is not None and path.suffix.lower() in da.VIDEO_EXT
        attrs = f'id="{aid}" name={quoteattr(path.stem)} start="0s" duration="{dur}"'
        attrs += f' hasVideo="1" format="{fmt_for(info)}"' if video else ' hasVideo="0"'
        if info["has_audio"]:
            attrs += ' hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000"'
        res.append(f'<asset {attrs}><media-rep kind="original-media" src={quoteattr(path.as_uri())}/></asset>')
        assets[path] = (aid, info)
        return assets[path]

    # ---- spine clips (tight cuts, full media available as handles)
    clips, pos = [], 0
    for seg in segments:
        s_f = tb.frames(seg["start"])
        d_f = max(1, tb.frames(seg["end"]) - s_f)
        aid, _ = asset(seg["path"])
        clips.append({"seg": seg, "aid": aid, "offset": pos, "start": s_f, "dur": d_f, "children": []})
        pos += d_f
    total = pos

    def attach(t_frames, xml):
        """Connect an item at timeline frame t to the spine clip covering it."""
        t_frames = max(0, min(t_frames, total - 1))
        c = next(c for c in clips if c["offset"] <= t_frames < c["offset"] + c["dur"])
        c["children"].append(xml.format(local=tb.t(c["start"] + t_frames - c["offset"])))

    whoosh_len = tb.frames(da.probe(whoosh)["duration"]) if whoosh else 0
    whoosh_id = asset(whoosh)[0] if whoosh else None
    transitions = []
    for k, c in enumerate(clips):
        seg = c["seg"]
        if seg.get("mp3"):
            mid, minfo = asset(seg["mp3"])
            off = tb.frames(seg["offset"])
            d = min(tb.frames(minfo["duration"]), c["dur"] - off)
            if d > 0:
                c["children"].append(f'<asset-clip ref="{mid}" lane="-1" offset="{tb.t(c["start"] + off)}" '
                                     f'start="0s" duration="{tb.t(d)}" name={quoteattr(seg["mp3"].stem)}/>')
        if seg["left"] and k > 0:
            cut = c["offset"]
            half = tb.frames(TRANSITION_DURATION) // 2
            transitions.append((k, cut - half, 2 * half))
            c["children"].append(f'<marker start="{tb.t(c["start"])}" duration="{tb.t(1)}" value="Slide left"/>')
            if whoosh_id:
                attach(cut - whoosh_len // 2,
                       f'<asset-clip ref="{whoosh_id}" lane="-2" offset="{{local}}" start="0s" '
                       f'duration="{tb.t(whoosh_len)}" name="whoosh"/>')

    # ---- spine xml
    spine, trans_at = [], {k: (o, d) for k, o, d in transitions}
    for k, c in enumerate(clips):
        if k in trans_at:
            o, d = trans_at[k]
            spine.append(f'<transition name="Cross Dissolve" offset="{tb.t(o)}" duration="{tb.t(d)}">'
                         f'<filter-video ref="rx" name="Cross Dissolve"/></transition>')
        inner = []
        if c["seg"]["mute"]:
            inner.append('<adjust-volume amount="-96dB"/>')
        inner += c["children"]
        spine.append(f'<asset-clip ref="{c["aid"]}" offset="{tb.t(c["offset"])}" start="{tb.t(c["start"])}" '
                     f'duration="{tb.t(c["dur"])}" name={quoteattr(c["seg"]["path"].stem)} tcFormat="NDF">'
                     + "".join(inner) + "</asset-clip>")

    name = folder.name
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.9">\n'
           '<resources>\n' + "\n".join(res) + '\n</resources>\n'
           f'<library><event name={quoteattr(name)}><project name={quoteattr(name)}>'
           f'<sequence format="r0" duration="{tb.t(total)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">'
           '<spine>\n' + "\n".join(spine) + '\n</spine></sequence></project></event></library>\n</fcpxml>\n')
    out = folder / f"{name}_timeline.fcpxml"
    out.write_text(xml, encoding="utf-8")
    return out, total / tb.fps, len(transitions)


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python resolve_timeline.py /path/to/folder")
    if not shutil.which("ffprobe"):
        sys.exit("FFmpeg not found. Install it and make sure 'ffprobe' works in a terminal.")
    out, secs, n = build(sys.argv[1])
    print(f"\nTimeline written: {out}\n  {float(secs):.1f}s, {n} slide transitions")
    print("In Resolve: File > Import > Timeline..., choose this file.")


if __name__ == "__main__":
    main()
