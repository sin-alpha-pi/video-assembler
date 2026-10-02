"""Dialogue Assembler - window app (tkinter) around core.py."""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import core

HELP = """How it works
1. Put everything for one video in a folder:
   - the script as a .txt file
   - speaker clips with the speaker name in the filename (mentor_1.mp4, husband_2.mp4, ...)
   - transition.mp3 (the whoosh), intro/background clips and their mp3
2. Tags in the script (own line or before a line):
   [intro.mp4]                          whole clip, muted
   [left]                               slide-left + whoosh into the next item
   [background.mp4 + background.mp3]    whole clip, muted, with the mp3
   [background.mp4 + background.mp3 @1.2]   mp3 starts 1.2 s into the clip
3. Click Analyze, check the cut list, then click Render video.
   The video is saved in the same folder as <foldername>_assembled.mp4.
"""


class App:
    def __init__(self, root, initial=None):
        self.root = root
        self.q = queue.Queue()
        self.plan = None
        self.busy = False
        root.title("Dialogue Assembler")
        root.geometry("900x640")
        root.minsize(700, 480)

        pad = {"padx": 10, "pady": 6}
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Folder:").pack(side="left")
        self.folder = tk.StringVar(value=initial or "")
        ttk.Entry(top, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Choose folder...", command=self.choose).pack(side="left")

        sett = ttk.Frame(root)
        sett.pack(fill="x", **pad)
        d = core.Settings()
        self.vars = {}
        for key, label, val in [("transition", "Slide (s)", d.transition),
                                ("padding", "Padding (s)", d.padding),
                                ("silence_db", "Silence (dB)", d.silence_db),
                                ("mp3_offset", "mp3 start (s)", d.mp3_offset),
                                ("whoosh_volume", "Whoosh vol.", d.whoosh_volume)]:
            ttk.Label(sett, text=label).pack(side="left")
            v = tk.StringVar(value=str(val))
            ttk.Entry(sett, textvariable=v, width=6).pack(side="left", padx=(4, 14))
            self.vars[key] = v

        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.b_analyze = ttk.Button(btns, text="Analyze", command=self.analyze)
        self.b_analyze.pack(side="left")
        self.b_render = ttk.Button(btns, text="Render video", command=self.render, state="disabled")
        self.b_render.pack(side="left", padx=6)
        self.b_open = ttk.Button(btns, text="Open folder", command=self.open_folder)
        self.b_open.pack(side="left")
        ttk.Button(btns, text="Help", command=lambda: self.show(HELP, clear=True)).pack(side="right")

        self.bar = ttk.Progressbar(root, maximum=1000)
        self.bar.pack(fill="x", padx=10)
        self.status = ttk.Label(root, text="Choose a folder, then click Analyze.")
        self.status.pack(fill="x", padx=10, pady=(4, 0))

        self.out = ScrolledText(root, wrap="word", font=("Consolas", 10) if os.name == "nt" else ("Menlo", 11))
        self.out.pack(fill="both", expand=True, padx=10, pady=10)
        self.out.tag_config("bad", foreground="#c43232")
        self.out.tag_config("warn", foreground="#b8730a")
        self.out.tag_config("ok", foreground="#1f8a4c")
        self.out.tag_config("head", font=("Segoe UI", 11, "bold") if os.name == "nt" else ("Helvetica", 13, "bold"))
        self.show(HELP)
        root.after(100, self.poll)

    # ---------------------------------------------------------- helpers
    def choose(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or None)
        if d:
            self.folder.set(d)
            self.plan = None
            self.b_render.config(state="disabled")

    def open_folder(self):
        f = self.folder.get()
        if f and Path(f).is_dir():
            if os.name == "nt":
                os.startfile(f)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", f])

    def show(self, text, tag=None, clear=False):
        if clear:
            self.out.delete("1.0", "end")
        self.out.insert("end", text + "\n", tag or ())
        self.out.see("end")

    def settings(self):
        s = core.Settings()
        for k, v in self.vars.items():
            try:
                setattr(s, k, float(v.get()))
            except ValueError:
                raise RuntimeError(f"Setting '{k}' must be a number.")
        return s

    def set_busy(self, b):
        self.busy = b
        self.b_analyze.config(state="disabled" if b else "normal")
        self.b_render.config(state="normal" if (not b and self.plan and self.plan.ok) else "disabled")

    def start(self, fn):
        if self.busy:
            return
        self.set_busy(True)
        self.bar["value"] = 0

        def worker():
            try:
                fn()
            except Exception as e:  # report every failure in the window
                self.q.put(("error", str(e) or e.__class__.__name__, traceback.format_exc()))
            finally:
                self.q.put(("done",))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                kind = m[0]
                if kind == "log":
                    self.status.config(text=m[1])
                    self.show(m[1])
                elif kind == "progress":
                    self.bar["value"] = m[1] * 1000
                elif kind == "status":
                    self.status.config(text=m[1])
                elif kind == "plan":
                    self.show_plan(m[1])
                elif kind == "error":
                    self.status.config(text="Error: " + m[1].splitlines()[0])
                    self.show("\nERROR: " + m[1], "bad")
                    self.show(m[2], "warn")
                elif kind == "done":
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # ---------------------------------------------------------- actions
    def analyze(self):
        folder = self.folder.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showinfo("Dialogue Assembler", "Choose the folder with the clips and script first.")
            return
        self.plan = None
        self.show("Analyzing " + folder, "head", clear=True)
        s = self.settings()

        def job():
            plan = core.analyze(folder, s, log=lambda t: self.q.put(("log", t)),
                                progress=lambda r: self.q.put(("progress", r)))
            self.plan = plan
            self.q.put(("plan", plan))
        self.start(job)

    def show_plan(self, plan):
        self.show("\nCut list  ('>' = slide-left in)", "head")
        for r in plan.rows:
            self.show(f"{r['n']:>3} {'>' if r['left'] else ' '} {r['label']}")
            self.show(f"      {r['src']}", r["level"] if r["level"] != "ok" else None)
            for lvl, t in r["notes"]:
                self.show(f"      {t}", lvl)
        if plan.ok:
            self.status.config(text=f"All {len(plan.rows)} items found. Check the list, then click Render video.")
            self.show("\nAll items found. Click Render video.", "ok")
        else:
            self.status.config(text=f"{plan.problems} line(s) not found - see the cut list.")
            self.show(f"\n{plan.problems} line(s) not found. Check the red lines above.", "bad")
        self.bar["value"] = 1000

    def render(self):
        if not self.plan or not self.plan.ok:
            return
        s = self.settings()
        out = core.output_path(self.folder.get())
        self.show("\nRendering...", "head")

        def job():
            total, n = core.render(self.plan, out, s, log=lambda t: self.q.put(("log", t)),
                                   progress=lambda r: self.q.put(("progress", r)))
            self.q.put(("log", f"Done: {out.name} ({total:.1f}s, {n} transitions)"))
        self.start(job)


def selftest(folder, result_file):
    """Headless run used by the automated build check."""
    try:
        s = core.Settings()
        plan = core.analyze(folder, s)
        text = plan.text()
        if not plan.ok:
            raise RuntimeError("lines not found:\n" + text)
        total, n = core.render(plan, core.output_path(folder), s)
        Path(result_file).write_text(f"OK {total:.2f}s {n} transitions\n{text}", encoding="utf-8")
        return 0
    except Exception:
        Path(result_file).write_text("FAIL\n" + traceback.format_exc(), encoding="utf-8")
        return 1


def demo(folder, shot_dir):
    """Opens the real window, clicks Analyze and Render, saves screenshots (used by the build)."""
    from PIL import ImageGrab
    shot_dir = Path(shot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root, folder)
    root.geometry("900x640+20+20")
    root.attributes("-topmost", True)
    result = {"code": 1}

    def shot(name):
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        ImageGrab.grab(bbox=(x - 8, y - 32, x + root.winfo_width() + 8, y + root.winfo_height() + 8),
                       all_screens=True).save(shot_dir / f"{name}.png")

    def wait_idle(then):
        if app.busy:
            root.after(500, lambda: wait_idle(then))
        else:
            then()

    def step1():
        shot("1_start")
        app.analyze()
        root.after(3000, lambda: (shot("2_analyzing"), wait_idle(step2)))

    def step2():
        shot("3_cut_list")
        if not (app.plan and app.plan.ok):
            root.destroy()
            return
        app.render()
        root.after(4000, lambda: (shot("4_rendering"), wait_idle(step3)))

    def step3():
        shot("5_done")
        result["code"] = 0 if core.output_path(folder).exists() else 1
        root.destroy()

    root.after(2000, step1)
    root.after(900000, root.destroy)  # safety stop after 15 min
    root.mainloop()
    return result["code"]


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 4 and sys.argv[1] == "--demo":
        sys.exit(demo(sys.argv[2], sys.argv[3]))
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    initial = sys.argv[1] if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir() else None
    App(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
