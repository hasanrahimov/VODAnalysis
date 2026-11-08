#!/usr/bin/env python3
"""
Template extractor for killfeed icons.

Open a screenshot, draw a box around the icon you want, press ENTER — it saves
the crop as a template. You need TWO templates:

    templates/icon_kill.png   — the crosshair/skull icon shown for a KILL
    templates/icon_dbno.png   — the down-arrow icon shown for a DBNO (knock)

Usage:
    python make_template.py hud_samples/02_action.png --name icon_kill
    python make_template.py hud_samples/02_action.png --name icon_dbno

Controls in the window:
    - drag a rectangle around the icon
    - ENTER / SPACE to confirm
    - C to cancel

Tip: crop tightly around just the icon, no surrounding text. The template is
saved in grayscale (matching how the reader compares it).
"""

import argparse
import os
import sys

import cv2

import config


def main():
    ap = argparse.ArgumentParser(description="Extract a killfeed icon template")
    ap.add_argument("screenshot", help="Path to a screenshot containing the icon")
    ap.add_argument("--name", required=True,
                    help="Template name, e.g. icon_kill or icon_dbno")
    ap.add_argument("--out", default=config.TEMPLATE_DIR,
                    help=f"Output dir (default: {config.TEMPLATE_DIR})")
    args = ap.parse_args()

    if not os.path.exists(args.screenshot):
        print(f"ERROR: screenshot not found: {args.screenshot}")
        sys.exit(1)

    img = cv2.imread(args.screenshot)
    if img is None:
        print(f"ERROR: could not read image: {args.screenshot}")
        sys.exit(1)

    print("Draw a tight box around the icon, then press ENTER (or SPACE).")
    print("Press C to cancel.")

    # selectROI returns (x, y, w, h)
    roi = cv2.selectROI("Select icon — ENTER to save, C to cancel", img,
                        showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()

    x, y, w, h = roi
    if w == 0 or h == 0:
        print("Cancelled — no region selected.")
        sys.exit(0)

    crop = img[y:y + h, x:x + w]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    os.makedirs(args.out, exist_ok=True)
    name = args.name if args.name.endswith(".png") else args.name + ".png"
    out_path = os.path.join(args.out, name)
    cv2.imwrite(out_path, gray)

    print(f"Saved template: {out_path}  ({w}x{h} px)")
    print(f"Box: x={x} y={y} w={w} h={h}")


if __name__ == "__main__":
    main()
