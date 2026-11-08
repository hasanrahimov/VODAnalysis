"""
Plant (defuser) detection from the center notification banner.

Two notifications matter, each visible ~3 seconds (hence FRAME_INTERVAL_SEC<=3):
  START   "<name>.PLR is activating the defuser"   -> who is planting
  CONFIRM "Your team has activated the defuser"    -> plant succeeded

A plant can be interrupted (defused) — there is no separate trigger for that,
so we infer: START seen but CONFIRM never seen within the round  ->  interrupted.

read_plant_event() returns a dict:
  {"state": "start"|"confirm"|None, "ours": bool, "planter": str|None, "raw": str}
"""

import re
from typing import Dict, Optional

import numpy as np

import config
from regions_loader import crop_named
from ocr_utils import ocr_text, fuzzy_match

_NICK_TOKEN_RE = re.compile(r"([A-Za-z0-9_]{2,})\.([A-Za-z0-9]{1,5})")


def _contains_any(text: str, keywords) -> bool:
    return any(k.lower() in text for k in keywords)


def _extract_planter(text: str) -> Optional[str]:
    """Pull the '<name>.TAG' token from a start notification."""
    m = _NICK_TOKEN_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}"


def read_plant_event(img_bgr: np.ndarray, profile: dict,
                     raw: str = None) -> Dict:
    """
    Inspect the plant banner for a start/confirm notification.
    Pass *raw* (pre-OCR'd banner text from get_banner_texts) to skip a
    duplicate OCR pass.
    """
    if raw is None:
        crop = crop_named(img_bgr, profile, "plant_banner")
        if crop is None or crop.size == 0:
            return {"state": None, "ours": False, "planter": None, "raw": ""}
        raw = ocr_text(crop, psm=6)
    text = raw.lower()

    # CONFIRM (check first — "activated" vs "activating")
    if _contains_any(text, config.PLANT_CONFIRM_KEYWORDS):
        ours = _contains_any(text, config.PLANT_OURS_KEYWORDS)
        return {"state": "confirm", "ours": ours, "planter": None, "raw": raw}

    # START
    if _contains_any(text, config.PLANT_START_KEYWORDS):
        planter = _extract_planter(raw)
        ours = False
        if planter:
            tag = planter.split(".")[-1].upper()
            ours = (tag == config.OUR_TEAM_TAG.upper()
                    or fuzzy_match(tag, [config.OUR_TEAM_TAG.upper()], min_score=80)[0] is not None)
        return {"state": "start", "ours": ours, "planter": planter, "raw": raw}

    return {"state": None, "ours": False, "planter": None, "raw": raw}
