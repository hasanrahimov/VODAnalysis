#!/usr/bin/env python3
"""
HUD Region Calibrator
=====================

Usage:
    python calibrate.py <screenshot.png> [--yaml regions.yaml] [--out out/]

Workflow:
    1. Take a 1080p screenshot from your R6M VOD at the moment you want to
       calibrate (operator-pick / mid-round / round-end).
    2. Edit `regions.yaml` — adjust x/y/w/h for each region.
    3. Run this script — it produces:
         out/annotated.jpg        — original with all rectangles drawn
         out/crops/<region>.jpg   — each region cropped out
         out/phase_detectors.txt  — mean RGB of detector regions
    4. Open out/annotated.jpg, adjust YAML, re-run, repeat.

Dependencies: pip install pillow pyyaml
"""

import argparse
import os
import sys
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

import config

# Windows consoles default to cp1251 here — allow unicode (✓, →) in plain prints.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# Cycle of colors for drawing region rectangles
_COLORS = [
    (255,  64,  64),   # red
    ( 64, 255,  64),   # green
    ( 64, 128, 255),   # blue
    (255, 200,   0),   # yellow
    (255,   0, 255),   # magenta
    (  0, 255, 255),   # cyan
    (255, 128,   0),   # orange
    (180,  80, 255),   # purple
    (255, 255, 255),   # white
    (128, 255, 128),   # light green
]


def _load_config(yaml_path: str) -> dict:
    with open(yaml_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _select_profile(cfg: dict, profile_name: str | None) -> tuple[str, dict]:
    """
    Resolve which profile to use.

    Supports two YAML layouts:
      A) Multi-profile:  { default_profile: <name>, profiles: { <name>: {...} } }
      B) Legacy flat:    { profile: <name>, regions: {...}, phase_detectors: {...} }
    """
    if "profiles" in cfg:
        profiles = cfg["profiles"]
        name = profile_name or cfg.get("default_profile") or next(iter(profiles))
        if name not in profiles:
            avail = ", ".join(profiles.keys())
            raise KeyError(f"Profile '{name}' not found. Available: {avail}")
        return name, profiles[name]

    # Legacy flat layout — single profile
    return cfg.get("profile", "default"), cfg


def _load_font(size: int = 18) -> ImageFont.ImageFont:
    """Try several font paths; fall back to default."""
    candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _resolve(region: dict, w_img: int, h_img: int):
    """Region dict → pixel (x, y, w, h). Fractional (<=1) or pixel coords."""
    x, y, w, h = region["x"], region["y"], region["w"], region["h"]
    if max(x, y, w, h) <= 1.0:
        x, y, w, h = x * w_img, y * h_img, w * w_img, h * h_img
    return int(round(x)), int(round(y)), int(round(w)), int(round(h))


def _crop(img: Image.Image, region: dict) -> Image.Image:
    w_img, h_img = img.size
    x, y, w, h = _resolve(region, w_img, h_img)
    return img.crop((x, y, x + w, y + h))


def _mean_rgb(img: Image.Image, region: dict) -> tuple:
    crop = _crop(img, region).convert("RGB")
    px = list(crop.getdata())
    n = len(px)
    if n == 0:
        return (0, 0, 0)
    r = sum(p[0] for p in px) // n
    g = sum(p[1] for p in px) // n
    b = sum(p[2] for p in px) // n
    return (r, g, b)


def _draw_label(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
                color: tuple, font: ImageFont.ImageFont):
    """Draw a filled label box with text."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = 4

    # Background box above the region
    bg_y = max(0, y - th - pad * 2 - 2)
    draw.rectangle(
        [x, bg_y, x + tw + pad * 2, bg_y + th + pad * 2],
        fill=color,
    )
    # Choose text color for contrast
    brightness = (color[0] * 299 + color[1] * 587 + color[2] * 114) / 1000
    text_color = (0, 0, 0) if brightness > 128 else (255, 255, 255)
    draw.text((x + pad, bg_y + pad), text, fill=text_color, font=font)


def _open_image(path: str):
    """Open image with default OS viewer (non-blocking)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass  # Silent fail — user can open manually


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Crop and annotate HUD regions defined in regions.yaml"
    )
    ap.add_argument("screenshot", nargs="?", help="Path to a 1080p screenshot to test against")
    ap.add_argument("--yaml", default=config.REGIONS_YAML,
                    help="Path to regions YAML (default: data/regions.yaml)")
    ap.add_argument("--profile", default=None,
                    help="Profile name to use (default: default_profile in YAML)")
    ap.add_argument("--list-profiles", action="store_true",
                    help="List available profiles and exit")
    ap.add_argument("--out", default=config.OUT_DIR,
                    help="Output directory (default: out/)")
    ap.add_argument("--no-open", action="store_true",
                    help="Don't auto-open the annotated image when done")
    args = ap.parse_args()

    if not os.path.exists(args.yaml):
        print(f"ERROR: regions YAML not found: {args.yaml}")
        sys.exit(1)

    cfg = _load_config(args.yaml)

    # --list-profiles mode
    if args.list_profiles:
        if "profiles" in cfg:
            default = cfg.get("default_profile", "(none)")
            print(f"Available profiles (default: {default}):")
            for name in cfg["profiles"].keys():
                marker = " *" if name == default else "  "
                print(f"{marker} {name}")
        else:
            print(f"Legacy flat layout, single profile: {cfg.get('profile', 'default')}")
        sys.exit(0)

    if not args.screenshot:
        ap.error("screenshot argument is required (unless --list-profiles)")

    if not os.path.exists(args.screenshot):
        print(f"ERROR: screenshot not found: {args.screenshot}")
        sys.exit(1)

    try:
        profile_name, profile = _select_profile(cfg, args.profile)
    except KeyError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    regions = profile.get("regions", {})
    detect  = profile.get("phase_detectors", {})
    print(f"Using profile: [{profile_name}]")

    img = Image.open(args.screenshot).convert("RGB")
    print(f"Loaded screenshot: {img.size[0]}x{img.size[1]} "
          f"(native — fractional coords, no resize)")

    out_dir   = Path(args.out) / profile_name
    crops_dir = out_dir / "crops"
    out_dir.mkdir(parents=True, exist_ok=True)
    crops_dir.mkdir(exist_ok=True)

    # 1) Crop each region
    print(f"\nCropping {len(regions)} regions →  {crops_dir}/")
    W_img, H_img = img.size
    for name, r in regions.items():
        crop = _crop(img, r)
        path = crops_dir / f"{name}.jpg"
        crop.save(path, quality=90)
        bx, by, bw, bh = _resolve(r, W_img, H_img)
        print(f"  ✓ {name:25s} px=({bx},{by}) {bw}x{bh}  → {path.name}")
        desc = r.get("description", "")
        if desc:
            print(f"      {desc}")

    # 2) Annotated overlay
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    font = _load_font(20)

    for i, (name, r) in enumerate(regions.items()):
        color = _COLORS[i % len(_COLORS)]
        x, y, w, h = _resolve(r, annotated.size[0], annotated.size[1])
        draw.rectangle([x, y, x + w, y + h], outline=color, width=4)
        _draw_label(draw, x, y, name, color, font)

    annotated_path = out_dir / "annotated.jpg"
    annotated.save(annotated_path, quality=92)
    print(f"\nAnnotated overlay →  {annotated_path}")

    # 3) Phase detector probes
    if detect:
        report_lines = [
            "Phase detector probe results",
            "=" * 60,
            f"Source: {args.screenshot}",
            "",
        ]
        print(f"\nPhase detectors ({len(detect)}):")
        print("-" * 60)
        for name, d in detect.items():
            region_name = d.get("region")
            if region_name not in regions:
                msg = f"  ✗ {name}: region '{region_name}' not found in regions"
                print(msg)
                report_lines.append(msg)
                continue
            region = regions[region_name]
            mean   = _mean_rgb(img, region)
            target = tuple(d.get("target_rgb", [0, 0, 0]))
            tol    = d.get("tolerance", 50)
            dist   = max(abs(mean[i] - target[i]) for i in range(3))
            matches = dist <= tol
            status  = "✓ MATCH" if matches else "✗ no match"

            line = (
                f"  {name:30s} region={region_name:20s} "
                f"mean=({mean[0]:3d},{mean[1]:3d},{mean[2]:3d}) "
                f"target=({target[0]:3d},{target[1]:3d},{target[2]:3d}) "
                f"dist={dist:3d}  tol={tol:<3d}  {status}"
            )
            print(line)
            report_lines.append(line)

        report_path = out_dir / "phase_detectors.txt"
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        print(f"\nFull report →  {report_path}")

    # 4) Open annotated image
    if not args.no_open:
        _open_image(str(annotated_path))
        print(f"\nOpened {annotated_path} in default viewer.")
        print("Edit regions.yaml and re-run this script to iterate.")


if __name__ == "__main__":
    main()
