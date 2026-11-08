# R6M VOD Analyzer

Extracts per-round statistics from **Rainbow Six Mobile** match recordings and
produces a paste-ready table for a stats spreadsheet.

It runs entirely on your machine, with no paid APIs: **OpenCV** handles image
preprocessing and killfeed icon matching, and **Tesseract OCR** reads the on-screen text.

> **Status:** work in progress. The pipeline has been tuned on a limited set of
> recordings from one custom HUD, so check the output before relying on it.

## What it extracts

| Automatic | Entered manually |
|---|---|
| Kills and deaths per player (confirmed kills only; knocks are ignored) | Operators |
| Opening kill / opening death (OK/OD), who, and the round timer at that moment | Site |
| Round result and running score | Strategy |
| Side, defuser plant and planter | 1vX clutches |
| Traded deaths and per-round KOST | |

Each round is exported as a tab-separated row matching the spreadsheet columns.
Manual fields are left blank.

## How it works

1. **Frame sampling:** `ffmpeg` extracts one frame every N seconds.
2. **Per-frame reading (parallel):** each HUD region is cropped using fractional
   coordinates, so layouts do not depend on resolution or aspect ratio.
3. **Killfeed parsing:** Tesseract segments the feed into rows, template matching
   classifies each row's icon as a kill or a knock, and names are fuzzy-matched
   against the rosters.
4. **Round reduction (sequential):** frames are grouped into rounds, repeated
   killfeed entries are removed, and OK/OD, trades, score and KOST are computed.

## Requirements

- Windows (commands below use PowerShell)
- Python 3.10+ with Tkinter (included in the standard Windows installer)
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
- [FFmpeg](https://ffmpeg.org/)

```powershell
pip install -r requirements.txt
winget install UB-Mannheim.TesseractOCR
winget install Gyan.FFmpeg
```

If Tesseract is installed in a non-default location:

```powershell
$env:TESSERACT_CMD="C:\path\to\tesseract.exe"
```

## Usage

### Desktop app

Double-click `run_app.bat`, or run `python src\app.py`.

| Tab | Purpose |
|---|---|
| **Analyze** | Choose a source and match details, run the analysis, then copy the TSV. |
| **Calibrate** | Load a frame and adjust HUD regions with sliders and a live preview. |
| **Test OCR** | Run every reader on one frame to see what it detects. |

### Command line

```powershell
python src\analyze_local.py --video "record.mp4" `
  --start 00:05:30 --end 00:38:00 `
  --start-side ATK --vs ACND --map Bank --date 20.06.2026 `
  --lineup "MrBom.AB,Hopeless.AB,Saiin.AB,UnkxwnTT,Tora.AB" `
  --enemy "Magneto,SeiKo.FX,ACND.Sway,ACND.Gav,ACND.Specify" `
  --interval 2
```

Omitted match details are requested in the terminal. Other sources:

```powershell
python src\analyze_local.py --frames .\hud_samples
python src\analyze_local.py --url "https://youtu.be/XXXX" --start 01:12:30 --end 01:38:45
```

| Option | Description |
|---|---|
| `--frames`, `--video`, `--url` | Input source |
| `--start`, `--end` | Segment to analyze (`HH:MM:SS`) |
| `--profile` | HUD profile from `data/regions.yaml` |
| `--interval` | Seconds between sampled frames (default: 3) |
| `--start-side` | Side in round 1: `ATK` or `DEF` |
| `--vs`, `--map`, `--date` | Match details for the export |
| `--lineup` | Five in-game nicks, in the order of `SHEET_PLAYERS` |
| `--enemy` | Enemy nicks, comma-separated |
| `--workers` | Parallel OCR workers (default: up to 8) |
| `--debug` | Save analyzed frames and a per-frame OCR log |

**For best accuracy, always provide `--enemy`.** Write all nicks exactly as they
appear in the killfeed. Every name is then matched to a known player, and
unrecognized text is discarded.

## Output

Files are saved to `out/<profile>/reports/`:

| File | Contents |
|---|---|
| `sheet_<timestamp>.tsv` | Paste-ready spreadsheet rows |
| `report_<timestamp>.txt` | Console report (CLI runs) |
| `rounds_<timestamp>.json` | Raw per-round data |

With debug enabled, `out/<profile>/debug_<timestamp>/` contains every analyzed
frame and `frames_debug.tsv`.

## Calibrating a new HUD

Calibration is needed once per HUD layout. The **Calibrate** tab is the easiest
way to do it. From the command line:

1. Capture a frame at the recording's native resolution:

   ```powershell
   ffmpeg -ss 00:10:00 -i "record.mp4" -frames:v 1 -q:v 2 hud_samples/native.png
   ```

2. Preview the regions and adjust `data/regions.yaml` (values are fractions from 0 to 1):

   ```powershell
   python src\calibrate.py hud_samples/native.png
   ```

3. Cut killfeed icon templates from a frame that shows each icon:

   ```powershell
   python src\make_template.py hud_samples/native.png --name icon_kill
   python src\make_template.py hud_samples/native.png --name icon_bodykill
   python src\make_template.py hud_samples/native.png --name icon_crosshair
   python src\make_template.py hud_samples/native.png --name icon_dbno
   ```

4. Check the result:

   ```powershell
   python src\test_ocr.py hud_samples/native.png
   ```

To add a profile, copy an existing block under `profiles:` in `data/regions.yaml`,
rename it, and adjust its coordinates. More detail is in
[docs/SETUP_LOCAL.md](docs/SETUP_LOCAL.md).

## Configuration

The main settings are in `src/config.py`:

| Setting | Description |
|---|---|
| `TESSERACT_CMD` | Path to the Tesseract executable |
| `FRAME_INTERVAL_SEC` | Default sampling interval |
| `SHEET_PLAYERS` | Player columns in the spreadsheet |
| `ROSTER_NICKS` | Default in-game nick for each player column |
| `OUR_TEAM_TAG` | Team tag used to identify your players |
| `TRADE_WINDOW_SEC` | Time window for counting a traded death |
| `DEDUP_WINDOW_SEC` | Time window for removing repeated killfeed entries |
| `TEMPLATE_KILL_ICONS`, `TEMPLATE_DBNO_ICONS` | Icon template files |
| `TEMPLATE_MATCH_THRESHOLD` | Minimum icon match score |

## Project structure

```text
run_app.bat            Launches the desktop app
requirements.txt       Python dependencies
data/regions.yaml      HUD profiles and region coordinates
templates/             Killfeed icon templates
hud_samples/           Sample frames for calibration and testing
docs/SETUP_LOCAL.md    Detailed setup and calibration guide
src/
  app.py               Desktop app
  analyze_local.py     Analysis engine and CLI
  killfeed_reader.py   Killfeed parsing and name matching
  phase_detector.py    Phase, side, round result and score reading
  timer_reader.py      Round timer reading
  plant_reader.py      Defuser plant detection
  ocr_utils.py         Image preprocessing and Tesseract helpers
  dedup.py             Killfeed de-duplication
  sheet_export.py      TSV export
  sheets_writer.py     Google Sheets writer (not connected to the app yet)
  video_processor.py   Video download and frame extraction
  console_report.py    Console tables and reports
  regions_loader.py    HUD profile loading and cropping
  calibrate.py         Region calibration tool
  make_template.py     Icon template cutter
  test_ocr.py          Single-frame diagnostics
  config.py            Settings
```
