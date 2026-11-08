"""Load HUD region definitions from regions.yaml and crop frames."""

from typing import Optional, Tuple

import cv2
import numpy as np
import yaml

import config


def normalize_frame(img: np.ndarray) -> np.ndarray:
    """
    No-op kept for backward compatibility.

    We now use FRACTIONAL region coordinates (see resolve_box), so frames are
    cropped at their native resolution — no resizing/stretching needed. This
    makes the pipeline resolution- and aspect-ratio-independent.
    """
    return img


def resolve_box(region: dict, w_img: int, h_img: int) -> tuple:
    """
    Convert a region dict to absolute pixel (x, y, w, h) for an image of size
    (w_img, h_img).

    Fractional mode (recommended): if every coordinate is <= 1.0 the values are
    treated as fractions of the frame (x*w_img, y*h_img, ...).
    Pixel mode: otherwise the values are used as-is (legacy).
    """
    x, y, w, h = region["x"], region["y"], region["w"], region["h"]
    if max(x, y, w, h) <= 1.0:           # fractional
        x, y, w, h = x * w_img, y * h_img, w * w_img, h * h_img
    x = max(0, int(round(x)))
    y = max(0, int(round(y)))
    w = int(round(w))
    h = int(round(h))
    return x, y, w, h


def load_profile(yaml_path: str, profile_name: Optional[str] = None) -> Tuple[str, dict]:
    """
    Return (profile_name, profile_dict).

    Supports the multi-profile layout:
        { default_profile: <name>, profiles: { <name>: {regions, phase_detectors} } }
    and the legacy flat layout (single profile at root).
    """
    with open(yaml_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if "profiles" in cfg:
        profiles = cfg["profiles"]
        name = profile_name or cfg.get("default_profile") or next(iter(profiles))
        if name not in profiles:
            avail = ", ".join(profiles.keys())
            raise KeyError(f"Profile '{name}' not found. Available: {avail}")
        return name, profiles[name]

    return cfg.get("profile", "default"), cfg


def get_region(profile: dict, name: str) -> Optional[dict]:
    """Return the region dict for *name*, or None if not defined."""
    return profile.get("regions", {}).get(name)


def crop(img: np.ndarray, region: dict) -> np.ndarray:
    """
    Crop an OpenCV BGR image to a region dict {x, y, w, h}.
    Supports fractional (0..1) or pixel coordinates; clamped to image bounds.
    """
    h_img, w_img = img.shape[:2]
    x, y, w, h = resolve_box(region, w_img, h_img)
    x2 = min(w_img, x + w)
    y2 = min(h_img, y + h)
    return img[y:y2, x:x2]


def crop_named(img: np.ndarray, profile: dict, name: str) -> Optional[np.ndarray]:
    """Crop by region name; returns None if the region isn't defined."""
    region = get_region(profile, name)
    if region is None:
        return None
    return crop(img, region)
