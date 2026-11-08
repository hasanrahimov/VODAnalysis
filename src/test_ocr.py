#!/usr/bin/env python3
"""
OCR / region tester — run the full local pipeline on ONE screenshot and show
exactly what each reader produces. Use this to tune regions.yaml, OCR
preprocessing, and template thresholds before processing a whole VOD.

Usage:
    python test_ocr.py hud_samples/02_action.png
    python test_ocr.py hud_samples/01_op_pick.png --profile mrbom_default

Outputs to out/<profile>/ocr_test/:
    <region>.png          raw crop
    <region>_prep.png     preprocessed (what Tesseract actually sees)
And prints OCR text + phase detection + killfeed classification to console.
"""

import argparse
import os
import sys
from pathlib import Path

# Console may be cp1251 (Windows) — force UTF-8 so '→', nicks, etc. don't crash
# when output is piped/redirected.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import cv2

import config
from regions_loader import load_profile, crop_named, normalize_frame
from ocr_utils import ocr_text, preprocess, fuzzy_match, clean_nick
from phase_detector import detect_phase, round_result, read_round_number
from killfeed_reader import read_killfeed, templates_available
from timer_reader import read_timer
from plant_reader import read_plant_event

from rich.console import Console
from rich.table import Table
from rich import box as rbox

console = Console()


def _save(img, path):
    if img is not None and img.size > 0:
        cv2.imwrite(str(path), img)


def main():
    ap = argparse.ArgumentParser(description="Test OCR/regions on one screenshot")
    ap.add_argument("screenshot")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--slot3", default="Player3")
    ap.add_argument("--slot4", default="Player4")
    args = ap.parse_args()

    if not os.path.exists(args.screenshot):
        console.print(f"[red]Screenshot not found: {args.screenshot}[/red]")
        sys.exit(1)

    img = cv2.imread(args.screenshot)
    if img is None:
        console.print(f"[red]Could not read image[/red]")
        sys.exit(1)
    orig_shape = (img.shape[1], img.shape[0])
    img = normalize_frame(img)

    profile_name, prof = load_profile(config.REGIONS_YAML, args.profile)
    console.print(f"Profile: [bold]{profile_name}[/bold]   "
                  f"Image: {orig_shape[0]}x{orig_shape[1]} → normalized "
                  f"{img.shape[1]}x{img.shape[0]}\n")

    out_dir = Path(config.OUT_DIR) / profile_name / "ocr_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase ────────────────────────────────────────────────────────────
    from phase_detector import read_side_hint, objective_plant_state, read_score
    phase = detect_phase(img, prof)
    console.print(f"[bold]Detected phase:[/bold] [cyan]{phase}[/cyan]")
    console.print(f"  side (objective):   {read_side_hint(img, prof)}")
    console.print(f"  round_result probe: {round_result(img, prof)}")
    console.print(f"  round_number probe: {read_round_number(img, prof)}")
    console.print(f"  score probe:        {read_score(img, prof)}")
    console.print(f"  obj plant state:    {objective_plant_state(img, prof)}")
    pe = read_plant_event(img, prof)
    console.print(f"  plant notif:        state={pe['state']} ours={pe['ours']} "
                  f"planter={pe['planter']}  [dim](raw: {pe['raw']!r})[/dim]\n")

    # ── Per-region raw OCR ───────────────────────────────────────────────
    table = Table(title="Region OCR", box=rbox.SIMPLE_HEAVY, header_style="bold cyan")
    table.add_column("Region")
    table.add_column("Raw OCR")
    table.add_column("Saved crop")

    for region_name in prof.get("regions", {}).keys():
        crop = crop_named(img, prof, region_name)
        if crop is None or crop.size == 0:
            table.add_row(region_name, "[dim](empty)[/dim]", "")
            continue

        _save(crop, out_dir / f"{region_name}.png")
        _save(preprocess(crop), out_dir / f"{region_name}_prep.png")

        raw = clean_nick(ocr_text(crop, psm=6))
        table.add_row(region_name, raw or "[dim]—[/dim]", f"{region_name}.png")

    console.print(table)

    # ── Preprocessing previews (so you can SEE what OCR reads & tune) ─────
    kf = crop_named(img, prof, "killfeed")
    if kf is not None and kf.size:
        for ch, inv in (("white", True), ("sat", False), ("min", True), ("luma", False)):
            _save(preprocess(kf, channel=ch, invert=inv),
                  out_dir / f"killfeed_{ch}{'_inv' if inv else ''}.png")
    for sreg in ("roundnum1", "roundnum2"):
        sc = crop_named(img, prof, sreg)
        if sc is not None and sc.size:
            _save(preprocess(sc, channel="score", invert=True),
                  out_dir / f"{sreg}_score.png")
            _save(preprocess(sc, channel="score", invert=False),
                  out_dir / f"{sreg}_score_inv.png")
    console.print(f"[dim]Saved killfeed_*/roundnum*_score previews to {out_dir} — "
                  f"open them; tune WHITE_TEXT_THRESH / ADAPTIVE_* in config.[/dim]")

    # ── Specialized readers ──────────────────────────────────────────────
    console.print("\n[bold]Specialized readers:[/bold]")
    console.print(f"  timer:     [yellow]{read_timer(img, prof)}[/yellow]")

    known = list(config.ROSTER_NICKS)
    if not templates_available():
        console.print(
            "\n[yellow]Killfeed templates missing (templates/icon_kill.png + "
            "icon_dbno.png) — make them with make_template.py to read kills.[/yellow]"
        )
    # Per-row diagnostics (scores + decision) — tune TEMPLATE_MATCH_THRESHOLD
    from killfeed_reader import debug_rows
    rows = debug_rows(img, prof, known)
    if rows:
        console.print("\n[bold]Killfeed rows (kill/dbno scores → decision):[/bold]")
        for r in rows:
            console.print(f"  row {r['row']}: kill={r['kill']} dbno={r['dbno']} "
                          f"→ {r['decision']}   [dim]{r['text']!r}[/dim]")

    events = read_killfeed(img, prof, known)
    console.print(f"\n[bold]Killfeed ({len(events)} kill rows accepted):[/bold]")
    for ev in events:
        if ev.our_kill:
            tag = "[green]OUR KILL[/green]"
        elif ev.our_death:
            tag = "[red]OUR DEATH[/red]"
        elif ev.is_first_blood:
            tag = "[cyan]FIRST BLOOD[/cyan]"
        else:
            tag = "[dim]other[/dim]"
        console.print(f"  {ev.killer} → {ev.victim}   {tag}   [dim](raw: {ev.raw})[/dim]")

    console.print(f"\n[dim]Crops + preprocessed images saved to {out_dir}/[/dim]")
    console.print("[dim]Open the *_prep.png files to see what Tesseract sees — "
                  "if text is broken there, tune OCR_UPSCALE/OCR_INVERT in config.py "
                  "or the region box in regions.yaml.[/dim]")


if __name__ == "__main__":
    main()
