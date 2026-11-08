#!/usr/bin/env python3
"""
R6M VOD Analyzer — desktop GUI (Tkinter).

Three tabs:
  • Analyze    — pick a video/folder/URL, set options, run full analysis,
                 see the round summary + warnings, auto-save report.
  • Calibrate  — load a frame, pick a region, drag sliders to fit it (live
                 preview), read its OCR, save fractions back to regions.yaml.
  • Test OCR   — load a frame, run every reader, see annotated regions + all
                 OCR output (phase, plant, killfeed, timer, score).

Thin UI over the existing pipeline — no analysis logic is duplicated here.

Run:  python app.py
"""

import os
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageTk

import config
from regions_loader import load_profile, resolve_box, crop as crop_region
from ocr_utils import ocr_text
from phase_detector import (
    get_banner_texts, detect_phase, round_result, read_round_number,
    read_side_hint, read_score,
)
from killfeed_reader import read_killfeed, templates_available
from timer_reader import read_timer
from plant_reader import read_plant_event
from analyze_local import analyze_frames, _list_profiles, _canonical
from video_processor import extract_frames, download_segment

# Optional: round-trip YAML (preserves comments). Fall back to pyyaml.
try:
    from ruamel.yaml import YAML
    _ruamel = YAML()
    _ruamel.preserve_quotes = True
    _ruamel.indent(mapping=2, sequence=4, offset=2)
except Exception:
    _ruamel = None


# Distinct box colors per region (RGB)
_COLORS = [
    (255, 64, 64), (64, 255, 64), (64, 128, 255), (255, 200, 0),
    (255, 0, 255), (0, 255, 255), (255, 128, 0), (180, 80, 255),
    (255, 255, 255), (128, 255, 128),
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _font(size=16):
    for p in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _cv_to_pil(img_bgr):
    return Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))


def _draw_regions(img_bgr, regions, selected=None, scale=1.0):
    """Return a scaled PIL image with all region boxes drawn (selected = thick)."""
    pil = _cv_to_pil(img_bgr)
    if scale != 1.0:
        pil = pil.resize((int(pil.width * scale), int(pil.height * scale)),
                         Image.LANCZOS)
    draw = ImageDraw.Draw(pil)
    font = _font(15)
    W, H = img_bgr.shape[1], img_bgr.shape[0]
    for i, (name, r) in enumerate(regions.items()):
        color = _COLORS[i % len(_COLORS)]
        x, y, w, h = resolve_box(r, W, H)
        x, y, w, h = x * scale, y * scale, w * scale, h * scale
        width = 4 if name == selected else 2
        draw.rectangle([x, y, x + w, y + h], outline=color, width=int(width))
        draw.text((x + 2, max(0, y - 16)), name, fill=color, font=font)
    return pil


def _save_region_coords(yaml_path, profile_name, region_name, x, y, w, h):
    """Write x/y/w/h (fractions) back to regions.yaml, preserving comments if possible."""
    if _ruamel is not None:
        with open(yaml_path, encoding="utf-8") as f:
            data = _ruamel.load(f)
        node = (data["profiles"][profile_name]["regions"][region_name]
                if "profiles" in data else data["regions"][region_name])
        node["x"] = round(float(x), 4)
        node["y"] = round(float(y), 4)
        node["w"] = round(float(w), 4)
        node["h"] = round(float(h), 4)
        with open(yaml_path, "w", encoding="utf-8") as f:
            _ruamel.dump(data, f)
    else:
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        node = (data["profiles"][profile_name]["regions"][region_name]
                if "profiles" in data else data["regions"][region_name])
        node.update({"x": round(float(x), 4), "y": round(float(y), 4),
                     "w": round(float(w), 4), "h": round(float(h), 4)})
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


# ===========================================================================
# Main application
# ===========================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("R6M VOD Analyzer")
        self.geometry("1180x820")

        self.profiles, self.default_profile = _list_profiles(config.REGIONS_YAML)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.analyze_tab = ttk.Frame(nb)
        self.calib_tab = ttk.Frame(nb)
        self.test_tab = ttk.Frame(nb)
        nb.add(self.analyze_tab, text="  Analyze  ")
        nb.add(self.calib_tab, text="  Calibrate  ")
        nb.add(self.test_tab, text="  Test OCR  ")

        self._build_analyze_tab()
        self._build_calibrate_tab()
        self._build_test_tab()

        self.q = queue.Queue()
        self.after(120, self._poll_queue)

        if not templates_available():
            self.after(300, lambda: messagebox.showwarning(
                "Killfeed templates missing",
                "templates/icon_kill.png, icon_kill2.png, icon_dbno.png not found.\n"
                "Kills won't be read until you create them (Calibrate tab → "
                "or make_template.py)."))

    # -------------------------------------------------------------------
    # ANALYZE TAB
    # -------------------------------------------------------------------
    def _build_analyze_tab(self):
        f = self.analyze_tab

        top = ttk.LabelFrame(f, text="Source")
        top.pack(fill="x", padx=8, pady=6)

        self.src_kind = tk.StringVar(value="video")
        for i, (val, label) in enumerate([
            ("video", "Video file (phone recording)"),
            ("frames", "Frames folder"),
            ("url", "YouTube URL"),
        ]):
            ttk.Radiobutton(top, text=label, variable=self.src_kind,
                            value=val).grid(row=0, column=i, sticky="w", padx=6, pady=4)

        self.src_path = tk.StringVar()
        ttk.Entry(top, textvariable=self.src_path, width=80).grid(
            row=1, column=0, columnspan=3, sticky="we", padx=6)
        ttk.Button(top, text="Browse…", command=self._browse_source).grid(
            row=1, column=3, padx=6)
        top.columnconfigure(0, weight=1)

        opt = ttk.LabelFrame(f, text="Options")
        opt.pack(fill="x", padx=8, pady=6)

        from datetime import date as _date
        self.a_profile = tk.StringVar(value=self.default_profile or self.profiles[0])
        self.a_side = tk.StringVar(value="ATK")
        self.a_vs = tk.StringVar()
        self.a_map = tk.StringVar()
        self.a_date = tk.StringVar(value=_date.today().strftime("%d.%m.%Y"))
        self.a_start = tk.StringVar()
        self.a_end = tk.StringVar()
        self.a_interval = tk.StringVar(value=str(config.FRAME_INTERVAL_SEC))
        self.a_workers = tk.StringVar(value=str(min(8, os.cpu_count() or 4)))

        def cell(r, c, label, widget):
            ttk.Label(opt, text=label).grid(row=r, column=c, sticky="e", padx=6, pady=3)
            widget.grid(row=r, column=c + 1, sticky="w", padx=6, pady=3)

        # Left column pair (cols 0-1)
        cell(0, 0, "Profile", ttk.Combobox(opt, textvariable=self.a_profile,
                                           values=self.profiles, width=22, state="readonly"))
        cell(1, 0, "Start side (R1)", ttk.Combobox(opt, textvariable=self.a_side,
                                                   values=["ATK", "DEF"], width=8, state="readonly"))
        cell(2, 0, "VS (opponent)", ttk.Entry(opt, textvariable=self.a_vs, width=18))
        cell(3, 0, "Map", ttk.Entry(opt, textvariable=self.a_map, width=18))

        # Right column pair (cols 2-3)
        cell(0, 2, "Date", ttk.Entry(opt, textvariable=self.a_date, width=12))
        cell(1, 2, "Start / End", ttk.Entry(opt, textvariable=self.a_start, width=12))
        cell(2, 2, "End (HH:MM:SS)", ttk.Entry(opt, textvariable=self.a_end, width=12))
        cell(3, 2, "Interval (s)", ttk.Entry(opt, textvariable=self.a_interval, width=8))
        cell(0, 4, "Workers", ttk.Entry(opt, textvariable=self.a_workers, width=8))

        # Lineup: one in-game nick per role (default core roster)
        self.a_lineup = []
        lf = ttk.LabelFrame(f, text="Lineup (in-game nick per role — change for flex subs)")
        lf.pack(fill="x", padx=8, pady=(0, 6))
        for i, role in enumerate(config.SHEET_PLAYERS):
            ttk.Label(lf, text=role).grid(row=0, column=i * 2, sticky="e", padx=(8, 2), pady=4)
            var = tk.StringVar(value=config.ROSTER_NICKS[i] if i < len(config.ROSTER_NICKS) else "")
            ttk.Entry(lf, textvariable=var, width=12).grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 6))
            self.a_lineup.append(var)

        # Enemy roster (optional but recommended): comma-separated in-game nicks.
        # When given, every killfeed name is snapped to one of the 10 known nicks
        # and anything else is rejected — fewer misreads.
        self.a_enemy = tk.StringVar(value="")
        ef = ttk.LabelFrame(f, text="Enemy roster (comma-separated nicks — optional, improves accuracy)")
        ef.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Entry(ef, textvariable=self.a_enemy, width=80).pack(fill="x", padx=8, pady=4)

        ctl = ttk.Frame(f); ctl.pack(fill="x", padx=8, pady=4)
        self.run_btn = ttk.Button(ctl, text="▶ Run Analysis", command=self._start_analysis)
        self.run_btn.pack(side="left")
        self.a_debug = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Debug frames", variable=self.a_debug).pack(side="left", padx=8)
        self.a_status = tk.StringVar(value="Ready.")
        ttk.Label(ctl, textvariable=self.a_status).pack(side="left", padx=12)
        self.a_prog = ttk.Progressbar(ctl, length=320, mode="determinate")
        self.a_prog.pack(side="right")

        # Results: 3 stacked, drag-resizable panes (table / warnings / TSV).
        paned = ttk.PanedWindow(f, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=6)

        # --- Pane 1: results table with vertical + horizontal scrollbars ---
        tree_frame = ttk.Frame(paned)
        cols = ("#", "Side", "Score", "Res", "OK/OD", "Plant", "Kills", "Deaths", "KOST")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=10)
        widths = (40, 50, 60, 55, 90, 110, 220, 250, 200)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            # stretch=False keeps fixed widths so the H-scrollbar engages when the
            # total width exceeds the pane (and _autosize_columns can grow them).
            self.tree.column(c, width=w, anchor="w", stretch=False)
        tv_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tv_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tv_y.set, xscrollcommand=tv_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tv_y.grid(row=0, column=1, sticky="ns")
        tv_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        paned.add(tree_frame, weight=3)

        # --- Pane 2: warnings (own vertical scrollbar) ---
        warn_frame = ttk.Frame(paned)
        ttk.Label(warn_frame, text="Warnings / gray areas:").pack(anchor="w")
        wbox_frame = ttk.Frame(warn_frame); wbox_frame.pack(fill="both", expand=True)
        wy = ttk.Scrollbar(wbox_frame, orient="vertical")
        self.warn_box = tk.Text(wbox_frame, height=3, wrap="word", yscrollcommand=wy.set)
        wy.config(command=self.warn_box.yview)
        wy.pack(side="right", fill="y")
        self.warn_box.pack(side="left", fill="both", expand=True)
        paned.add(warn_frame, weight=1)

        # --- Pane 3: paste-ready TSV (horizontal + vertical scrollbars) ---
        tsv_frame = ttk.Frame(paned)
        tsv_bar = ttk.Frame(tsv_frame); tsv_bar.pack(fill="x")
        ttk.Label(tsv_bar, text="Paste-ready TSV (Sheets columns):").pack(side="left")
        ttk.Button(tsv_bar, text="📋 Copy TSV", command=self._copy_tsv).pack(side="right")
        tbox_frame = ttk.Frame(tsv_frame); tbox_frame.pack(fill="both", expand=True)
        xscroll = ttk.Scrollbar(tbox_frame, orient="horizontal")
        yscroll = ttk.Scrollbar(tbox_frame, orient="vertical")
        self.tsv_box = tk.Text(tbox_frame, height=6, wrap="none",
                               xscrollcommand=xscroll.set,
                               yscrollcommand=yscroll.set)
        xscroll.config(command=self.tsv_box.xview)
        yscroll.config(command=self.tsv_box.yview)
        yscroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")
        self.tsv_box.pack(side="left", fill="both", expand=True)
        paned.add(tsv_frame, weight=2)
        self._last_tsv = ""

    def _autosize_columns(self):
        """Grow each Treeview column to fit its widest cell/header (with a cap)."""
        import tkinter.font as tkfont
        font = tkfont.nametofont("TkDefaultFont")
        for c in self.tree["columns"]:
            w = font.measure(str(c)) + 24
            for iid in self.tree.get_children():
                w = max(w, font.measure(str(self.tree.set(iid, c))) + 24)
            self.tree.column(c, width=min(w, 600))

    def _browse_source(self):
        kind = self.src_kind.get()
        if kind == "frames":
            p = filedialog.askdirectory(title="Select frames folder")
        elif kind == "video":
            p = filedialog.askopenfilename(
                title="Select video", filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi"), ("All", "*.*")])
        else:
            return
        if p:
            self.src_path.set(p)

    def _start_analysis(self):
        kind = self.src_kind.get()
        src = self.src_path.get().strip()
        if kind in ("video", "frames") and not src:
            messagebox.showerror("No source", "Pick a source first.")
            return
        if kind == "url" and not src:
            messagebox.showerror("No URL", "Enter a YouTube URL.")
            return
        try:
            interval = int(self.a_interval.get())
            workers = int(self.a_workers.get())
        except ValueError:
            messagebox.showerror("Bad value", "Interval/Workers must be integers.")
            return

        self.run_btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.warn_box.delete("1.0", "end")
        self.a_prog.config(value=0)
        self.a_status.set("Starting…")

        lineup = [v.get().strip() or config.ROSTER_NICKS[i]
                  for i, v in enumerate(self.a_lineup)]
        enemy = [n.strip() for n in re.split(r"[,\n;]+", self.a_enemy.get())
                 if n.strip()]
        args = dict(
            kind=kind, src=src,
            profile=self.a_profile.get(),
            side=self.a_side.get(),
            lineup=lineup,
            enemy=enemy,
            vs=self.a_vs.get().strip(),
            map=self.a_map.get().strip(),
            date=self.a_date.get().strip(),
            start=self.a_start.get().strip() or None,
            end=self.a_end.get().strip() or None,
            interval=interval, workers=workers,
            debug=self.a_debug.get(),
        )
        threading.Thread(target=self._analysis_worker, args=(args,), daemon=True).start()

    def _analysis_worker(self, a):
        try:
            import glob, tempfile, shutil, json
            from datetime import datetime

            profile_name, prof = load_profile(config.REGIONS_YAML, a["profile"])

            temp_dir = None
            self.q.put(("status", "Obtaining frames…"))
            if a["kind"] == "frames":
                exts = ("*.png", "*.jpg", "*.jpeg", "*.bmp")
                frames = sorted(p for e in exts
                                for p in glob.glob(os.path.join(a["src"], e)))
            elif a["kind"] == "video":
                self.q.put(("status", "Extracting frames (ffmpeg)…"))
                frames = extract_frames(a["src"], interval_sec=a["interval"],
                                        start=a["start"], end=a["end"])
            else:
                temp_dir = tempfile.mkdtemp(prefix="r6m_gui_")
                seg = os.path.join(temp_dir, "segment.mp4")
                if not (a["start"] and a["end"]):
                    raise ValueError("YouTube needs both Start and End times.")
                self.q.put(("status", "Downloading…"))
                download_segment(a["src"], a["start"], a["end"], seg)
                self.q.put(("status", "Extracting frames…"))
                frames = extract_frames(seg, interval_sec=a["interval"])

            if not frames:
                raise ValueError("No frames found.")

            total = len(frames)

            def cb(done, tot):
                self.q.put(("progress", (done, tot)))

            self.q.put(("status", f"Analyzing {total} frames…"))
            debug_dir = None
            if a.get("debug"):
                debug_dir = os.path.join(
                    config.OUT_DIR, profile_name,
                    f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            timeline, rounds, warnings = analyze_frames(
                frames, prof, a["interval"], a["side"], a["lineup"],
                workers=a["workers"], progress_cb=cb, debug_dir=debug_dir,
                enemy=a.get("enemy"))

            # Save report + paste-ready TSV
            import sheet_export
            match = {"date": a["date"], "vs": a["vs"], "map": a["map"],
                     "start_side": a["side"]}
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = os.path.join(config.OUT_DIR, profile_name, "reports")
            os.makedirs(out_dir, exist_ok=True)
            json_path = os.path.join(out_dir, f"rounds_{stamp}.json")
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(rounds, fh, ensure_ascii=False, indent=2, default=str)
            tsv_path = os.path.join(out_dir, f"sheet_{stamp}.tsv")
            sheet_export.save_tsv(rounds, match, tsv_path)
            tsv_text = sheet_export.to_tsv(rounds, match)

            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

            self.q.put(("done", (rounds, warnings, json_path, tsv_path, tsv_text)))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.a_status.set(payload)
                elif kind == "progress":
                    done, tot = payload
                    self.a_prog.config(maximum=tot, value=done)
                    self.a_status.set(f"Analyzing… {done}/{tot}")
                elif kind == "done":
                    self._show_results(*payload)
                elif kind == "error":
                    messagebox.showerror("Analysis failed", payload)
                    self.a_status.set("Failed.")
                    self.run_btn.config(state="normal")
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _show_results(self, rounds, warnings, json_path, tsv_path=None, tsv_text=""):
        for r in rounds:
            score = r.get("score")
            score_s = f"{score[0]}-{score[1]}" if score else "—"
            okod = r.get("ok_od") or ""
            who = r.get("ok_od_who") or ""
            okod_s = f"{okod} {who}".strip() or "—"
            if r.get("plant"):
                plant_s = f"✓ {r.get('plant_by') or ''}".strip()
            elif r.get("plant_started"):
                plant_s = "start✗"
            else:
                plant_s = "—"
            kills_s = ", ".join(f"{n}×{c}" for n, c in r.get("our_kills", {}).items()) or "—"
            traded = r.get("traded", {})
            deaths = []
            for n, k in r.get("our_deaths", {}).items():
                s = f"{n}←{k}"
                if n in traded:
                    s += f" (T:{traded[n]})"
                deaths.append(s)
            deaths_s = "; ".join(deaths) or "—"
            kost_s = " ".join(f"{n}:{v}" for n, v in r.get("kost", {}).items()) or "—"
            self.tree.insert("", "end", values=(
                r.get("round", "?"), r.get("side", "?"), score_s,
                r.get("result", "?"), okod_s, plant_s, kills_s, deaths_s, kost_s))

        self._autosize_columns()    # fit columns to content

        if warnings:
            self.warn_box.insert("end", "\n".join(f"• {w}" for w in warnings))
        else:
            self.warn_box.insert("end", "No warnings.")

        # Paste-ready TSV + Copy button
        self.tsv_box.delete("1.0", "end")
        self.tsv_box.insert("end", tsv_text)
        self._last_tsv = tsv_text

        n_kills = sum(len(r["kills"]) for r in rounds)
        self.a_status.set(f"Done. {len(rounds)} rounds, {n_kills} kills.  "
                          f"TSV: {tsv_path}")
        self.run_btn.config(state="normal")

    def _copy_tsv(self):
        txt = getattr(self, "_last_tsv", "")
        if txt:
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.a_status.set("TSV copied to clipboard — paste into Google Sheets.")

    # -------------------------------------------------------------------
    # CALIBRATE TAB
    # -------------------------------------------------------------------
    def _build_calibrate_tab(self):
        f = self.calib_tab
        self.c_img = None       # native cv2 frame
        self.c_scale = 1.0
        self._c_photo = None

        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Load frame…", command=self._calib_load).pack(side="left")
        self.c_profile = tk.StringVar(value=self.default_profile or self.profiles[0])
        ttk.Label(bar, text="Profile:").pack(side="left", padx=(12, 2))
        cb = ttk.Combobox(bar, textvariable=self.c_profile, values=self.profiles,
                          width=18, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._calib_reload_regions())
        ttk.Label(bar, text="Region:").pack(side="left", padx=(12, 2))
        self.c_region = tk.StringVar()
        self.c_region_cb = ttk.Combobox(bar, textvariable=self.c_region, width=22,
                                        state="readonly")
        self.c_region_cb.pack(side="left")
        self.c_region_cb.bind("<<ComboboxSelected>>", lambda e: self._calib_load_region())

        body = ttk.Frame(f); body.pack(fill="both", expand=True, padx=8, pady=4)
        self.c_canvas = tk.Label(body, bg="#222")
        self.c_canvas.pack(side="left", fill="both", expand=True)

        side = ttk.Frame(body, width=300); side.pack(side="right", fill="y")
        self.c_sliders = {}
        for key in ("x", "y", "w", "h"):
            row = ttk.Frame(side); row.pack(fill="x", pady=3)
            ttk.Label(row, text=key, width=2).pack(side="left")
            var = tk.DoubleVar(value=0.0)
            s = ttk.Scale(row, from_=0.0, to=1.0, variable=var, length=180,
                          command=lambda v, k=key: self._calib_slider(k))
            s.pack(side="left")
            ent = ttk.Entry(row, width=7)
            ent.pack(side="left", padx=4)
            ent.bind("<Return>", lambda e, k=key: self._calib_entry(k))
            self.c_sliders[key] = (var, ent)

        ttk.Button(side, text="Read region (OCR)",
                   command=self._calib_ocr).pack(fill="x", pady=(10, 2))
        self.c_crop_label = tk.Label(side, bg="#333")
        self.c_crop_label.pack(fill="x", pady=4)
        self.c_ocr_text = tk.Text(side, height=4, width=34, wrap="word")
        self.c_ocr_text.pack(fill="x", pady=4)
        ttk.Button(side, text="💾 Save region to regions.yaml",
                   command=self._calib_save).pack(fill="x", pady=8)
        self.c_status = tk.StringVar(value="Load a native-resolution frame to start.")
        ttk.Label(side, textvariable=self.c_status, wraplength=280,
                  foreground="gray").pack(fill="x")

    def _calib_load(self):
        p = filedialog.askopenfilename(
            title="Load frame", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All", "*.*")])
        if not p:
            return
        img = cv2.imread(p)
        if img is None:
            messagebox.showerror("Error", "Could not read image.")
            return
        self.c_img = img
        self._calib_reload_regions()
        self.c_status.set(f"Loaded {img.shape[1]}x{img.shape[0]}")

    def _calib_profile_regions(self, profile_var):
        _, prof = load_profile(config.REGIONS_YAML, profile_var.get())
        return prof.get("regions", {})

    def _calib_reload_regions(self):
        regions = self._calib_profile_regions(self.c_profile)
        self.c_region_cb["values"] = list(regions.keys())
        if regions and not self.c_region.get():
            self.c_region.set(next(iter(regions)))
        self._calib_load_region()
        self._calib_redraw()

    def _calib_load_region(self):
        regions = self._calib_profile_regions(self.c_profile)
        r = regions.get(self.c_region.get())
        if not r:
            return
        for key in ("x", "y", "w", "h"):
            var, ent = self.c_sliders[key]
            val = float(r[key])
            var.set(val)
            ent.delete(0, "end"); ent.insert(0, f"{val:.4f}")
        self._calib_redraw()

    def _calib_slider(self, key):
        var, ent = self.c_sliders[key]
        ent.delete(0, "end"); ent.insert(0, f"{var.get():.4f}")
        self._calib_redraw()

    def _calib_entry(self, key):
        var, ent = self.c_sliders[key]
        try:
            v = max(0.0, min(1.0, float(ent.get())))
            var.set(v)
            self._calib_redraw()
        except ValueError:
            pass

    def _current_region_dict(self):
        return {k: self.c_sliders[k][0].get() for k in ("x", "y", "w", "h")}

    def _calib_redraw(self):
        if self.c_img is None:
            return
        regions = dict(self._calib_profile_regions(self.c_profile))
        sel = self.c_region.get()
        if sel:
            regions[sel] = self._current_region_dict()   # live override
        W, H = self.c_img.shape[1], self.c_img.shape[0]
        scale = min(880 / W, 560 / H, 1.0)
        self.c_scale = scale
        pil = _draw_regions(self.c_img, regions, selected=sel, scale=scale)
        self._c_photo = ImageTk.PhotoImage(pil)
        self.c_canvas.config(image=self._c_photo)

    def _calib_ocr(self):
        if self.c_img is None:
            return
        r = self._current_region_dict()
        crop = crop_region(self.c_img, r)
        if crop is None or crop.size == 0:
            return
        txt = ocr_text(crop, psm=6)
        self.c_ocr_text.delete("1.0", "end")
        self.c_ocr_text.insert("end", txt or "(empty)")
        # show crop thumbnail
        pil = _cv_to_pil(crop)
        s = min(260 / max(1, pil.width), 120 / max(1, pil.height))
        pil = pil.resize((max(1, int(pil.width * s)), max(1, int(pil.height * s))))
        self._c_crop_photo = ImageTk.PhotoImage(pil)
        self.c_crop_label.config(image=self._c_crop_photo)

    def _calib_save(self):
        sel = self.c_region.get()
        if not sel:
            return
        r = self._current_region_dict()
        try:
            _save_region_coords(config.REGIONS_YAML, self.c_profile.get(), sel,
                                r["x"], r["y"], r["w"], r["h"])
            self.c_status.set(f"Saved '{sel}' to regions.yaml")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # -------------------------------------------------------------------
    # TEST OCR TAB
    # -------------------------------------------------------------------
    def _build_test_tab(self):
        f = self.test_tab
        self.t_img = None
        self._t_photo = None

        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Load frame…", command=self._test_load).pack(side="left")
        self.t_profile = tk.StringVar(value=self.default_profile or self.profiles[0])
        ttk.Label(bar, text="Profile:").pack(side="left", padx=(12, 2))
        ttk.Combobox(bar, textvariable=self.t_profile, values=self.profiles,
                     width=18, state="readonly").pack(side="left")
        ttk.Button(bar, text="▶ Run", command=self._test_run).pack(side="left", padx=12)

        body = ttk.Frame(f); body.pack(fill="both", expand=True, padx=8, pady=4)
        self.t_canvas = tk.Label(body, bg="#222")
        self.t_canvas.pack(side="left", fill="both", expand=True)
        self.t_text = tk.Text(body, width=52, wrap="word")
        self.t_text.pack(side="right", fill="y")

    def _test_load(self):
        p = filedialog.askopenfilename(
            title="Load frame", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All", "*.*")])
        if not p:
            return
        img = cv2.imread(p)
        if img is None:
            messagebox.showerror("Error", "Could not read image.")
            return
        self.t_img = img
        self._test_draw()

    def _test_draw(self):
        if self.t_img is None:
            return
        _, prof = load_profile(config.REGIONS_YAML, self.t_profile.get())
        W, H = self.t_img.shape[1], self.t_img.shape[0]
        scale = min(820 / W, 560 / H, 1.0)
        pil = _draw_regions(self.t_img, prof.get("regions", {}), scale=scale)
        self._t_photo = ImageTk.PhotoImage(pil)
        self.t_canvas.config(image=self._t_photo)

    def _test_run(self):
        if self.t_img is None:
            messagebox.showinfo("No frame", "Load a frame first.")
            return
        self._test_draw()
        img = self.t_img
        _, prof = load_profile(config.REGIONS_YAML, self.t_profile.get())
        known = list(config.ROSTER_NICKS)

        texts = get_banner_texts(img, prof)
        out = []
        out.append(f"Image: {img.shape[1]}x{img.shape[0]}")
        out.append(f"Phase: {detect_phase(img, prof, texts)}")
        out.append(f"Round result: {round_result(img, prof, texts)}")
        out.append(f"Round number: {read_round_number(img, prof)}")
        out.append(f"Score: {read_score(img, prof)}")
        out.append(f"Side (objective): {read_side_hint(img, prof, texts)}")
        out.append(f"Timer: {read_timer(img, prof)}")
        from phase_detector import objective_plant_state
        out.append(f"Obj plant state: {objective_plant_state(img, prof, texts)}")
        pe = read_plant_event(img, prof, raw=texts.get('plant_banner'))
        out.append(f"Plant notif: state={pe['state']} ours={pe['ours']} planter={pe['planter']}")
        out.append("")
        out.append("── Per-region OCR ──")
        for name in prof.get("regions", {}):
            crop = crop_region(img, prof["regions"][name])
            txt = ocr_text(crop, psm=6) if crop is not None and crop.size else ""
            out.append(f"  {name}: {txt!r}")
        out.append("")
        if not templates_available():
            out.append("⚠ Killfeed templates missing — kills not read.")
        else:
            events = read_killfeed(img, prof, known)
            out.append(f"── Killfeed ({len(events)} rows) ──")
            for ev in events:
                tag = ("OUR KILL" if ev.our_kill else
                       "OUR DEATH" if ev.our_death else
                       "first-blood" if ev.is_first_blood else "other")
                out.append(f"  {ev.killer} → {ev.victim}  [{tag}]")

        self.t_text.delete("1.0", "end")
        self.t_text.insert("end", "\n".join(out))


if __name__ == "__main__":
    App().mainloop()
