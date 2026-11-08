"""
Shared OCR helpers: image preprocessing, Tesseract wrappers, fuzzy matching.
"""

from typing import List, Optional, Tuple

import cv2
import numpy as np
import pytesseract
from pytesseract import Output
from rapidfuzz import process, fuzz

import config

# Point pytesseract at the Tesseract binary
pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def _to_gray(img_bgr: np.ndarray, channel: str) -> np.ndarray:
    """
    BGR → single channel. Aim: make the TEXT bright, background dark.
      'luma'  : standard luminance
      'min'   : min(B,G,R) — white text bright, colored bg dark
      'max'   : max(B,G,R) (a.k.a. HSV Value) — brightest pixel wins
      'sat'   : HSV saturation — white text low, colored bg high (so we invert it
                so white text becomes bright)
    """
    if img_bgr.ndim == 2:
        return img_bgr
    if channel == "min":
        return img_bgr.min(axis=2).astype(np.uint8)
    if channel == "max":
        return img_bgr.max(axis=2).astype(np.uint8)
    if channel == "sat":
        s = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)[:, :, 1]
        return cv2.bitwise_not(s)   # white(low sat) → bright
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)


def preprocess(
    img_bgr: np.ndarray,
    upscale: int = None,
    invert: bool = None,
    threshold: bool = True,
    channel: str = "luma",
    adaptive: bool = False,
    morph: bool = True,
    border: bool = True,
) -> np.ndarray:
    """
    Crop → channel → upscale → binarize → (morph close) → (invert).
    Returns a single-channel uint8 binary image (dark text on light when invert).

    channel 'white' = fixed-threshold white-text mask (best for white-on-color).
    adaptive=True   = local threshold (best for same-hue text, e.g. blue-on-blue
                      score digits where the digit is just brighter than its bg).
    """
    if upscale is None:
        upscale = config.OCR_UPSCALE
    if invert is None:
        invert = config.OCR_INVERT
    if img_bgr is None or img_bgr.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    # --- channel → grayscale where TEXT is bright ---
    if channel == "white":
        gray = img_bgr.min(axis=2).astype(np.uint8) if img_bgr.ndim == 3 else img_bgr
    elif channel == "score" and img_bgr.ndim == 3:
        # Same-hue digit (blue-on-blue / red-on-red): take the bar's dominant
        # channel (the brighter digit shows there) and boost local contrast.
        means = img_bgr.reshape(-1, 3).mean(axis=0)   # B,G,R means
        gray = img_bgr[:, :, int(np.argmax(means))]
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    else:
        gray = _to_gray(img_bgr, channel)

    if upscale and upscale > 1:
        gray = cv2.resize(gray, None, fx=upscale, fy=upscale,
                          interpolation=cv2.INTER_CUBIC)

    # --- binarize: text → white(255), bg → black(0) ---
    if channel == "white":
        _, b = cv2.threshold(gray, config.WHITE_TEXT_THRESH, 255, cv2.THRESH_BINARY)
    elif adaptive:
        bs = config.ADAPTIVE_BLOCK | 1   # must be odd
        b = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY, bs, config.ADAPTIVE_C)
    elif threshold:
        _, b = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        b = gray

    # --- reconnect broken strokes (helps low-res text) ---
    is_binary = (channel == "white") or adaptive or threshold
    if morph and is_binary:
        k = np.ones((2, 2), np.uint8)
        b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, k)

    # Tesseract wants dark text on light background
    if invert:
        b = cv2.bitwise_not(b)

    # Pad a border — Tesseract reads tightly-cropped text (esp. a lone "1")
    # much better with whitespace around it.
    if border and b.ndim == 2 and b.size:
        pad = max(8, b.shape[0] // 3)
        bg = 255 if float(b.mean()) > 127 else 0   # pad with the background colour
        b = cv2.copyMakeBorder(b, pad, pad, pad, pad,
                               cv2.BORDER_CONSTANT, value=bg)
    return b


# ---------------------------------------------------------------------------
# Tesseract wrappers
# ---------------------------------------------------------------------------

def ocr_text(
    img_bgr: np.ndarray,
    psm: int = 7,
    whitelist: Optional[str] = None,
    upscale: int = None,
    invert: bool = None,
    threshold: bool = True,
    channel: str = "luma",
    adaptive: bool = False,
) -> str:
    """
    Run Tesseract on a crop and return stripped text.

    psm 7 = single line (nicks). psm 8 = single word. psm 10 = single char.
    channel = 'luma' | 'min' | 'max' | 'sat' | 'white' (see preprocess).
    adaptive = local threshold (same-hue text like blue-on-blue score).
    """
    prepped = preprocess(img_bgr, upscale=upscale, invert=invert,
                         threshold=threshold, channel=channel, adaptive=adaptive)
    cfg = f"--psm {psm} --oem 3"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    try:
        raw = pytesseract.image_to_string(prepped, config=cfg)
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError(
            f"Tesseract not found at '{config.TESSERACT_CMD}'.\n"
            "Install it (see SETUP_LOCAL.md) or set the TESSERACT_CMD env var."
        )
    return raw.strip()


def ocr_lines(
    img_bgr: np.ndarray,
    channel: str = "min",
    invert: bool = True,
    psm: int = 6,
) -> List[Tuple[str, int, int]]:
    """
    Segment a multi-line region into its text lines using Tesseract's OWN layout
    analysis (image_to_data), and return [(text, y1, y2), ...] in the ORIGINAL
    crop's pixel coordinates, sorted top→bottom.

    This is far more robust than fixed-height or projection splitting: it adapts
    to any HUD scale because Tesseract decides where the lines are. We preprocess
    with border=False so the only coordinate transform is the integer upscale.
    """
    if img_bgr is None or img_bgr.size == 0:
        return []
    up = config.OCR_UPSCALE or 1
    prepped = preprocess(img_bgr, upscale=up, invert=invert,
                         channel=channel, border=False)
    cfg = f"--psm {psm} --oem 3"
    try:
        data = pytesseract.image_to_data(prepped, config=cfg,
                                         output_type=Output.DICT)
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError(
            f"Tesseract not found at '{config.TESSERACT_CMD}'.\n"
            "Install it (see SETUP_LOCAL.md) or set the TESSERACT_CMD env var."
        )

    groups: dict = {}
    n = len(data["text"])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        g = groups.setdefault(key, {"words": [], "top": 10 ** 9, "bot": 0})
        g["words"].append((data["left"][i], txt))
        g["top"] = min(g["top"], data["top"][i])
        g["bot"] = max(g["bot"], data["top"][i] + data["height"][i])

    lines: List[Tuple[str, int, int]] = []
    for g in groups.values():
        g["words"].sort(key=lambda w: w[0])
        text = " ".join(w for _, w in g["words"])
        y1 = max(0, int(g["top"] / up))
        y2 = int(g["bot"] / up)
        lines.append((text, y1, y2))
    lines.sort(key=lambda t: t[1])
    return lines


# ---------------------------------------------------------------------------
# Fuzzy matching against known dictionaries
# ---------------------------------------------------------------------------

def fuzzy_match(
    text: str,
    candidates: List[str],
    min_score: int = None,
) -> Tuple[Optional[str], float]:
    """
    Return (best_candidate, score) if score >= min_score, else (None, score).
    Used to snap noisy OCR onto a known operator or teammate name.
    """
    if min_score is None:
        min_score = config.FUZZY_MIN_SCORE
    if not text or not candidates:
        return None, 0.0

    result = process.extractOne(text, candidates, scorer=fuzz.WRatio)
    if result is None:
        return None, 0.0
    match, score, _ = result
    return (match, score) if score >= min_score else (None, score)


def clean_nick(text: str) -> str:
    """
    Normalize an OCR'd nickname: strip stray whitespace and obvious junk chars,
    collapse internal spaces.
    """
    if not text:
        return ""
    # Keep letters, digits, dot, underscore, dash (typical nick chars)
    keep = []
    for ch in text:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        elif ch.isspace():
            keep.append(" ")
    return " ".join("".join(keep).split())


# ---------------------------------------------------------------------------
# Killfeed nick OCR — white-on-color / black-on-white, multi-variant
# ---------------------------------------------------------------------------

import re as _re
_NICK_TAG = _re.compile(r"[A-Za-z0-9_]{2,}\.[A-Za-z0-9]{1,5}")

# Preprocessing variants tried per nick crop (sensitivity knobs live here).
# Killfeed nicks are WHITE on a blue/red bar, or black on white.
NICK_OCR_VARIANTS = [
    {"channel": "white", "invert": True},   # fixed-threshold white-on-color (best)
    {"channel": "min",   "invert": True},   # white text on colored bar
    {"channel": "luma",  "invert": False},  # black text on white (e.g. markers)
]


def _nick_quality(text: str, known_bases: Optional[List[str]]) -> float:
    """Higher = more nick-like. Rewards a 'name.TAG' shape, length, known match."""
    if not text:
        return -1.0
    s = 0.0
    if _NICK_TAG.search(text):
        s += 50.0
    s += sum(ch.isalnum() for ch in text)
    if known_bases:
        base = text.split(".")[0]
        _, score = fuzzy_match(base, known_bases, min_score=0)
        s += score / 5.0
    return s


def ocr_nick(img_bgr, known_names: Optional[List[str]] = None, psm: int = 7) -> str:
    """
    OCR one killfeed nick crop, trying several preprocessings (white-on-color,
    black-on-white, both polarities) and returning the most nick-like result.
    """
    known_bases = ([n.split(".")[0] for n in known_names] if known_names else None)
    best, best_score = "", -1.0
    for v in NICK_OCR_VARIANTS:
        txt = clean_nick(ocr_text(img_bgr, psm=psm, **v))
        q = _nick_quality(txt, known_bases)
        if q > best_score:
            best, best_score = txt, q
    return best
