"""
Local phase detection.

  - op_pick   : color of the operator-select panel (free, no OCR)
  - round_end : OCR keywords in the center banner (WON / DEFEAT / ELIMINATED)
  - plant     : OCR the objective banner for 'defuser'
  - action    : default

Color-mean is unreliable for bright text on dark banners, so round_end / plant
use a tiny OCR pass on a small region (cheap).
"""

import re
from typing import Optional

import numpy as np

import config
from regions_loader import crop_named
from ocr_utils import ocr_text

# Phase constants
OP_PICK   = "op_pick"
PREP      = "prep"
ACTION    = "action"
ROUND_END = "round_end"
UNKNOWN   = "unknown"

# Round-end banner keywords. These must be DISTINCTIVE — a bare "WIN" matches
# garbage in the center-screen region on mid-round frames and causes a FALSE
# round-end (which splits the round and assigns a wrong result). Use only long,
# unambiguous words. "FLAWLESS"/"WLESS" cover the flawless-win banner.
_WIN_WORDS  = ("WON", "VICTORY", "ROUND WIN", "FLAWLESS", "WLESS")
_LOSS_WORDS = ("DEFEAT", "ELIMINATED", "LOST")


_BANNER_REGIONS = ("roundend_text", "objective_banner", "plant_banner")


def get_banner_texts(img_bgr: np.ndarray, profile: dict) -> dict:
    """
    OCR all banner regions ONCE per frame. Pass the result to detect_phase /
    round_result / read_side_hint / read_plant_event to avoid re-OCRing the
    same crops multiple times (major speedup, identical results).
    """
    texts = {}
    for name in _BANNER_REGIONS:
        texts[name] = _ocr_banner(crop_named(img_bgr, profile, name))
    return texts


# Banner text is white-on-color (objective/plant) OR bright-on-dark (round-end).
# Try a few preprocessings and keep the most letter-rich read.
_BANNER_VARIANTS = [
    {"channel": "min",  "invert": True},    # white text on colored bar
    {"channel": "luma", "invert": True},    # bright text on dark
    {"channel": "luma", "invert": False},
]


def _ocr_banner(crop_img) -> str:
    if crop_img is None or crop_img.size == 0:
        return ""
    best, best_n = "", -1
    for v in _BANNER_VARIANTS:
        t = ocr_text(crop_img, psm=6, **v)
        n = sum(c.isalpha() for c in t)
        if n > best_n:
            best, best_n = t, n
    return best


def _banner_text(img_bgr: np.ndarray, profile: dict,
                 texts: Optional[dict] = None) -> str:
    """Round-end center banner text, uppercased."""
    if texts is not None:
        return texts.get("roundend_text", "").upper()
    return _ocr_banner(crop_named(img_bgr, profile, "roundend_text")).upper()


def round_result(img_bgr: np.ndarray, profile: dict,
                 texts: Optional[dict] = None) -> str:
    """Return 'WIN', 'LOSS', or '?' from the center banner text."""
    text = _banner_text(img_bgr, profile, texts)
    if any(w in text for w in _WIN_WORDS):
        return "WIN"
    if any(w in text for w in _LOSS_WORDS):
        return "LOSS"
    return "?"


def is_round_end(img_bgr: np.ndarray, profile: dict,
                 texts: Optional[dict] = None) -> bool:
    return round_result(img_bgr, profile, texts) != "?"


def _objective_text(img_bgr: np.ndarray, profile: dict,
                    texts: Optional[dict] = None) -> str:
    """Under-timer objective banner (+ center banner) text for prep keywords."""
    if texts is not None:
        return (texts.get("objective_banner", "") + " "
                + texts.get("plant_banner", "")).lower()
    return (_ocr_banner(crop_named(img_bgr, profile, "objective_banner")) + " "
            + _ocr_banner(crop_named(img_bgr, profile, "plant_banner"))).lower()


def is_prep(img_bgr: np.ndarray, profile: dict,
            texts: Optional[dict] = None) -> bool:
    text = _objective_text(img_bgr, profile, texts)
    return any(k in text for k in config.OBJ_PREP_KEYWORDS)


def is_action_objective(img_bgr: np.ndarray, profile: dict,
                        texts: Optional[dict] = None) -> bool:
    """True if the objective banner shows an action-phase phrase."""
    text = _objective_text(img_bgr, profile, texts)
    return any(k in text for k in config.OBJ_ACTION_KEYWORDS)


def read_side_hint(img_bgr: np.ndarray, profile: dict,
                   texts: Optional[dict] = None) -> Optional[str]:
    """
    Return 'ATK'/'DEF' from the objective phrasing (works in prep AND action),
    or None. DEF checked first because 'defend the bombsite' (DEF) and
    'defend the defuser' (ATK) both contain 'defend'.
    """
    text = _objective_text(img_bgr, profile, texts)
    # ATK 'defend the defuser' must win over DEF generic 'defend'
    if "defend the defuser" in text:
        return "ATK"
    if any(k in text for k in config.OBJ_SIDE_DEF):
        return "DEF"
    if any(k in text for k in config.OBJ_SIDE_ATK):
        return "ATK"
    return None


def objective_plant_state(img_bgr: np.ndarray, profile: dict,
                          texts: Optional[dict] = None) -> Optional[str]:
    """
    Persistent plant state from the objective banner:
      'ours'  → "defend the defuser" (we are ATK and our defuser is planted)
      'enemy' → "disable the defuser" (we are DEF and enemy planted)
      None    → not planted / unknown
    More reliable than the 3-second plant notification (this stays all round).
    """
    text = _objective_text(img_bgr, profile, texts)
    if any(k in text for k in config.OBJ_PLANTED_OURS):
        return "ours"
    if any(k in text for k in config.OBJ_PLANTED_ENEMY):
        return "enemy"
    return None


def detect_phase(img_bgr: np.ndarray, profile: dict,
                 texts: Optional[dict] = None) -> str:
    """
    round_end → prep → action → op_pick.

    Phases are read from the objective banner text (free — we OCR it anyway):
      prep    = "fortify"/"search for bomb"/"position drone"/"preparation"
      action  = "defend"/"disable"/"escort"/"plant"/"pick up"/"defuser"/"bombsite"
      op_pick = none of the above (operator select / loading)
    Killfeed and plant are read ONLY during action.
    """
    if is_round_end(img_bgr, profile, texts):
        return ROUND_END
    if is_prep(img_bgr, profile, texts):
        return PREP
    if is_action_objective(img_bgr, profile, texts):
        return ACTION
    return OP_PICK


def _digits(img_bgr, profile, region) -> str:
    """
    Read a small numeric region (e.g. a single score digit).
    Single digits need psm 10 (single char); two-digit scores need psm 7/8.
    Score digits sit on a colored bar, so we try a few preprocessing combos and
    take the first that yields digits.
    """
    crop_img = crop_named(img_bgr, profile, region)
    if crop_img is None or crop_img.size == 0:
        return ""
    wl = "0123456789"
    # Score is same-hue (blue digit on blue bar / red on red). The 'score' method
    # takes the bar's dominant channel and boosts contrast (CLAHE), then Otsu —
    # the brighter digit separates.
    # IMPORTANT: psm 10 (single char) FAILS on a lone "1" in this HUD font —
    # psm 8 (single word) and psm 13 (raw line) read it reliably, so try those
    # first across channels/polarities, then fall back.
    attempts = [
        dict(psm=8,  channel="min",   invert=True),
        dict(psm=8,  channel="score", invert=True),
        dict(psm=8,  channel="score", invert=False),
        dict(psm=8,  channel="max",   invert=True),
        dict(psm=13, channel="min",   invert=True),
        dict(psm=13, channel="score", invert=True),
        dict(psm=8),                                   # default (luma, single word)
        dict(psm=8,  channel="score", adaptive=True, invert=True),
        dict(psm=7,  channel="score", invert=True),    # two-digit score
        dict(psm=6),                                   # plain luma block
        dict(psm=10, channel="score", invert=True),    # last-resort single char
    ]
    for kw in attempts:
        text = ocr_text(crop_img, whitelist=wl, **kw)
        d = re.sub(r"\D", "", text)
        if d:
            return d
    return ""


def read_round_number(img_bgr: np.ndarray, profile: dict) -> Optional[int]:
    """
    Round number. Prefers a 'ROUND N' label region; otherwise derives it from
    the two score digits (our_score + enemy_score + 1).
    """
    # 1) explicit ROUND N region
    crop_img = crop_named(img_bgr, profile, "roundnum")
    if crop_img is not None and crop_img.size > 0:
        text = ocr_text(crop_img, psm=7).upper()
        m = re.search(r"ROUND\s*(\d+)", text)
        if m:
            return int(m.group(1))

    # 2) derive from score digits (roundnum1 = our, roundnum2 = enemy)
    d1 = _digits(img_bgr, profile, "roundnum1")
    d2 = _digits(img_bgr, profile, "roundnum2")
    if d1.isdigit() and d2.isdigit():
        return int(d1) + int(d2) + 1

    return None


def read_score(img_bgr: np.ndarray, profile: dict) -> Optional[tuple]:
    """Return (our_score, enemy_score) from the two score digit regions."""
    d1 = _digits(img_bgr, profile, "roundnum1")
    d2 = _digits(img_bgr, profile, "roundnum2")
    if d1.isdigit() and d2.isdigit():
        return int(d1), int(d2)
    return None
