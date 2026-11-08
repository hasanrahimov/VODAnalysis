"""Round timer OCR (e.g. '1:45')."""

import re
from typing import Optional

import numpy as np

from regions_loader import crop_named
from ocr_utils import ocr_text

_TIMER_RE = re.compile(r"\b([0-5]?\d):([0-5]\d)\b")


def read_timer(img_bgr: np.ndarray, profile: dict) -> Optional[str]:
    """
    OCR the timer region. Returns 'M:SS' / 'MM:SS' or None.
    Digit+colon whitelist keeps Tesseract from inventing letters.
    """
    crop = crop_named(img_bgr, profile, "timer")
    if crop is None or crop.size == 0:
        return None

    # Timer is white-on-dark → 'min' channel reads it best; try a couple variants.
    for kw in (dict(channel="min", invert=True),
               dict(channel="luma", invert=True),
               dict()):
        raw = ocr_text(crop, psm=7, whitelist="0123456789:", **kw).replace(" ", "")
        m = _TIMER_RE.search(raw)
        if m:
            return f"{int(m.group(1))}:{m.group(2)}"
        # Fallback: 3-4 bare digits → MM:SS, but only if seconds are valid (<60)
        digits = re.sub(r"\D", "", raw)
        if len(digits) in (3, 4):
            mm, ss = digits[:-2], digits[-2:]
            if int(ss) < 60:
                return f"{int(mm)}:{ss}"
    return None
