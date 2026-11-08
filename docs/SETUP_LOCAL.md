# R6M VOD Analyzer — Local OCR Setup (Tesseract + OpenCV)

No paid API. Output is printed to the console; you copy it into Google Sheets
manually.

## 1. Python deps

```powershell
pip install -r requirements.txt
```

## 2. Install Tesseract OCR (the binary)

`pytesseract` is just a wrapper — the actual Tesseract program must be installed.

**Windows (recommended):**
```powershell
winget install UB-Mannheim.TesseractOCR
```
Default install path: `C:\Program Files\Tesseract-OCR\tesseract.exe` — already
set in `config.py`. If you installed elsewhere, set an env var:
```powershell
$env:TESSERACT_CMD="C:\your\path\tesseract.exe"
```

Verify:
```powershell
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
```

## 3. Install ffmpeg (only if downloading from URL)

```powershell
winget install Gyan.FFmpeg
```
Not needed if you feed the tool a folder of screenshots.

---

## The workflow (4 stages)

### Stage A — Calibrate regions

Coordinates in `regions.yaml` are **fractions of the frame (0..1)** — resolution-
and aspect-independent, no resizing. Use a **native-resolution** frame (e.g.
2688×1216 from the phone):

```powershell
ffmpeg -ss 00:10:00 -i "record.mp4" -frames:v 1 -q:v 2 hud_samples/native.png
python src\calibrate.py hud_samples/native.png
```
Open `out/<profile>/annotated.jpg`, adjust the fractional boxes in `regions.yaml`,
repeat until every region (killfeed, timer, objective_banner, plant_banner,
roundend_text, roundnum1/roundnum2, operator_grid) is framed correctly.

### Stage B — Make killfeed icon templates (3)

The icon between the two nicks is one of three: **crosshair** (kill), **skull**
(kill), or **down-arrow** (DBNO/knock). The reader template-matches it to skip
knocks and find the killer|victim split. Cut all three from a native-res frame:

```powershell
python src\make_template.py hud_samples/native.png --name icon_kill    # crosshair
python src\make_template.py hud_samples/native.png --name icon_kill2   # skull
python src\make_template.py hud_samples/native.png --name icon_dbno    # down-arrow (knock)
```
Drag a tight box around just the icon, press ENTER. Saves to `templates/`.
For `icon_dbno` you need a frame where the killfeed shows a **down-arrow** (knock).

> Templates must come from a frame at the **same resolution** you'll process
> (template matching is scale-sensitive).

Nicks themselves are read by OCR and classified by team suffix (`config.OUR_TEAM_TAG`).

### Stage C — Test OCR on single screenshots

```powershell
python src\test_ocr.py hud_samples/02_action.png
```
Shows: detected phase, plant probe, round number, raw OCR per region, timer,
killfeed kills (with OUR KILL / OUR DEATH tags).
Also saves `out/<profile>/ocr_test/<region>_prep.png` — **open these** to see
what Tesseract actually sees.

Tuning levers if OCR is wrong:
- Text cut off / box misplaced → fix the box in `regions.yaml`
- Text present but garbled in `_prep.png` → adjust `OCR_UPSCALE` / `OCR_INVERT`
  in `config.py`
- Operators/nicks slightly off → fuzzy matching usually fixes it; widen the
  operator list in `config.py` or lower `FUZZY_MIN_SCORE`
- DBNO rows counted as kills (or vice versa) → re-cut templates, or adjust
  `TEMPLATE_MATCH_THRESHOLD` in `config.py`

### Stage D — Run the full analysis

Run it bare and it asks for profile, side, players, and timecodes interactively:
```powershell
python src\analyze_local.py --video "C:\path\record.mp4"
```
The recording may start at lobby creation — it offers to trim to a time range.

Or pass everything as flags (no prompts):
```powershell
python src\analyze_local.py --video "record.mp4" --start 00:05:30 --end 00:38:00 `
  --start-side ATK --slot3 JLA --slot4 Trackz --profile tora_default
```

Other sources: `--frames .\hud_samples` (folder of images) or
`--url "https://youtu.be/XXXX" --start ... --end ...` (YouTube).

Output:
- **Frame Timeline** — every analyzed frame (phase, timer, kills) — the safety net
- **Round Summary** — aggregated per round, ready to copy into Sheets

Then copy the Round Summary into your Google Sheet by hand.

---

## Phase detection (cost control)

The pipeline only does expensive OCR where it makes sense:

| Phase       | What runs                                   |
|-------------|---------------------------------------------|
| op_pick     | nothing (operators entered manually)        |
| prep        | side from "Search for bombs"/"Fortify" text |
| action      | timer + killfeed + plant + score (dedup)    |
| round_end   | WIN/LOSS result (OCR keywords)              |

Phase is detected mostly via OCR keywords (round_end "WON/DEFEAT", prep
"Search for bombs"/"Fortify", plant "defuser"); op_pick uses a color detector
(`operator_pick_active` in `regions.yaml` — tune with calibrate.py mean-RGB).

## Deduplication

A killfeed entry lingers for several seconds, so the same kill appears in
multiple frames. `DEDUP_WINDOW_SEC` (config.py, default 8s) suppresses repeats
of the same killer→victim pair.

## Project layout

```
run_app.bat          launch the GUI (double-click)
src/                 all code (app.py, analyze_local.py, calibrate.py,
                     make_template.py, test_ocr.py, config.py, and the
                     core modules: phase_detector, killfeed_reader,
                     plant_reader, timer_reader, dedup, console_report,
                     regions_loader, ocr_utils, video_processor)
data/regions.yaml    HUD region coordinates (per profile, fractional 0..1)
templates/           icon_kill.png, icon_kill2.png, icon_dbno.png (you create)
hud_samples/         your screenshots for calibration
out/                 generated reports (auto-saved)
```

> **Easiest path: the GUI** (`run_app.bat`). The Calibrate tab replaces stages
> A–C with sliders + live preview; the Analyze tab replaces stage D.
> CLI commands above are the scriptable equivalent (run them as `python src\...`).
