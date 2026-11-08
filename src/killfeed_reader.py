"""
Killfeed reader = template matching + OCR + team-suffix parsing.

Killfeed (top-left) row format:   Killer.TAG  [icon]  Victim.TAG
The icon between the nicks is one of:
    crosshair  -> KILL          ) either is a kill
    skull      -> KILL          )
    down-arrow -> DBNO (knock)  -> skip

Special row: "Killer.TAG  [icon]  First Blood!" marks the opening kill.

Per row:
  1. template-match: best KILL icon vs best DBNO icon
        dbno wins  -> skip
        kill wins  -> it's a kill; icon x = killer|victim split
  2. if the row text says "First Blood" -> opening marker (killer only)
  3. else OCR left (killer) + right (victim), parse <base>.<TAG>
  4. classify our-kill / our-death by OUR_TEAM_TAG; snap our base to a teammate
"""

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

import config
from regions_loader import crop_named
from ocr_utils import ocr_text, ocr_nick, ocr_lines, fuzzy_match
from rapidfuzz import fuzz

_NICK_TOKEN_RE = re.compile(r"([A-Za-z0-9_]{2,})\.([A-Za-z0-9]{1,5})")


@dataclass
class KillEvent:
    killer: str
    victim: str
    killer_tag: str
    victim_tag: str
    our_kill: bool
    our_death: bool
    is_first_blood: bool
    fb_killer_ours: bool   # for first-blood rows: was the opening killer ours?
    raw: str


# ---------------------------------------------------------------------------
# Templates (cached)
# ---------------------------------------------------------------------------

_tmpl_cache = {}


def _load_template(filename: str) -> Optional[np.ndarray]:
    if filename not in _tmpl_cache:
        path = os.path.join(config.TEMPLATE_DIR, filename)
        _tmpl_cache[filename] = (
            cv2.imread(path, cv2.IMREAD_GRAYSCALE) if os.path.exists(path) else None
        )
    return _tmpl_cache[filename]


def _loaded(names: List[str]) -> List[np.ndarray]:
    return [t for t in (_load_template(n) for n in names) if t is not None]


def templates_available() -> bool:
    return len(_loaded(config.TEMPLATE_KILL_ICONS)) > 0


def _best_match(row_gray: np.ndarray, tmpls: List[np.ndarray],
                row_h: int = 0) -> Tuple[float, Optional[int]]:
    """
    Best (score, icon_center_x) across templates, tried at MULTIPLE SCALES so
    matching is resolution-independent (templates cut at one resolution still
    match frames at another).

    The killfeed icon is roughly as tall as the row, so we rescale each template
    to a few target heights relative to row_h and keep the best match.
    """
    best_score, best_x = 0.0, None
    rh, rw = row_gray.shape[:2]
    if rh == 0 or rw == 0:
        return 0.0, None

    # Target icon heights as fractions of the row height (icon ≈ row height)
    if row_h > 0:
        targets = [0.6 * row_h, 0.75 * row_h, 0.9 * row_h, 1.0 * row_h]
    else:
        targets = [rh]  # fallback: use template native size

    for tmpl in tmpls:
        if tmpl is None or tmpl.shape[0] == 0:
            continue
        scales = ([t / tmpl.shape[0] for t in targets] if row_h > 0 else [1.0])
        for sc in scales:
            if sc <= 0:
                continue
            th = max(6, int(round(tmpl.shape[0] * sc)))
            tw = max(6, int(round(tmpl.shape[1] * sc)))
            if th > rh or tw > rw:
                continue
            scaled = cv2.resize(tmpl, (tw, th), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(row_gray, scaled, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            # Polarity-invariant: the same icon shows as light-on-color OR
            # black-on-white. The inverted version correlates NEGATIVELY, so
            # take whichever magnitude is larger.
            if max_val >= -min_val:
                score, loc = max_val, max_loc
            else:
                score, loc = -min_val, min_loc
            if score > best_score:
                best_score = float(score)
                best_x = loc[0] + tw // 2
    return best_score, best_x


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _tag_is_ours(tag: str) -> bool:
    if not tag:
        return False
    if tag.upper() == config.OUR_TEAM_TAG.upper():
        return True
    return fuzzy_match(tag.upper(), [config.OUR_TEAM_TAG.upper()], min_score=80)[0] is not None


def _snap_teammate(base: str, known_names: List[str]) -> str:
    if not base or not known_names:
        return base
    bases = [n.split(".")[0] for n in known_names]
    return fuzzy_match(base, bases)[0] or base


def _parse_side(text: str, known_names: List[str]) -> Tuple[str, str, bool]:
    text = text.strip()
    m = _NICK_TOKEN_RE.search(text)
    if m:
        base, tag = m.group(1), m.group(2)
    else:
        words = re.findall(r"[A-Za-z0-9_]{2,}", text)
        base = max(words, key=len) if words else text
        tag = ""
    ours = _tag_is_ours(tag)
    # If the tag was dropped/garbled by OCR, fall back to matching the base
    # against this match's lineup nicks.
    if not ours and known_names:
        match, score = fuzzy_match(base, [n.split(".")[0] for n in known_names],
                                   min_score=82)
        ours = match is not None
    if ours:
        base = _snap_teammate(base, known_names)
    return base, tag, ours


def _is_first_blood(text: str) -> bool:
    low = text.lower()
    if any(k in low for k in config.FIRST_BLOOD_KEYWORDS):
        return True
    # OCR garbles "First Blood!" (e.g. "Frrst Bloodl", "First Bload") — accept a
    # fuzzy match too. Nobody has the nick "First Blood!", so this is safe.
    for k in config.FIRST_BLOOD_KEYWORDS:
        _, score = fuzzy_match(low, [k], min_score=0)
        if score >= 75:
            return True
    return False


def _is_non_nick(text: str) -> bool:
    low = text.lower().strip()
    if any(w in low for w in config.KILLFEED_NON_NICK_WORDS):
        return True
    # fuzzy: catch OCR-garbled marker words ("blood", "first blood", "headshot")
    for w in config.KILLFEED_NON_NICK_WORDS:
        _, score = fuzzy_match(low, [w], min_score=0)
        if score >= 80:
            return True
    return False


# ---------------------------------------------------------------------------
# Row detection (projection-based — adapts to any killfeed UI scale)
# ---------------------------------------------------------------------------

def _detect_rows(kf_bgr: np.ndarray, frame_h: int) -> List[Tuple[int, int]]:
    """
    Find entry bands in the killfeed by horizontal projection of the text/bar
    mask. Robust to killfeed UI scale (entries can be any height). Falls back to
    fixed-height splitting if nothing is found.
    """
    h, w = kf_bgr.shape[:2]
    mn = kf_bgr.min(axis=2) if kf_bgr.ndim == 3 else kf_bgr
    mask = (mn >= config.WHITE_TEXT_THRESH).astype(np.uint8)
    rowsum = mask.sum(axis=1)
    active = rowsum > (0.04 * w)               # ≥4% of width is bright (text/bar)

    min_h = max(8, int(0.022 * frame_h))       # ignore bands thinner than this
    gap = max(4, int(0.012 * frame_h))         # merge bands closer than this

    bands, start, in_band = [], 0, False
    for y in range(h):
        if active[y] and not in_band:
            start, in_band = y, True
        elif not active[y] and in_band:
            bands.append([start, y]); in_band = False
    if in_band:
        bands.append([start, h])

    merged = []
    for b in bands:
        if merged and b[0] - merged[-1][1] <= gap:
            merged[-1][1] = b[1]
        else:
            merged.append(b)

    out = [(max(0, s - 3), min(h, e + 3)) for s, e in merged if e - s >= min_h]
    if not out:                                # fallback: fixed rows
        rh = max(8, int(config.KILLFEED_ROW_HEIGHT_FRAC * frame_h))
        out = [(i * rh, min(h, (i + 1) * rh))
               for i in range(max(1, h // rh))]
    return out[:config.KILLFEED_MAX_ROWS]


# Channels tried for killfeed segmentation/reading, in no fixed priority — the
# best one is chosen per frame (white-on-color HUDs read best on 'white', but a
# different overlay/colour can favour 'min'; auto-picking is robust to both).
_KF_CHANNELS = [("white", True), ("min", True), ("luma", False)]


def _lines_quality(lines: List[Tuple[str, int, int]]) -> float:
    """Score a set of OCR'd lines by how nick-like they are (more real letters +
    'name.TAG' shapes = better). Used to auto-pick the killfeed channel."""
    if not lines:
        return -1.0
    score = 0.0
    for text, _y1, _y2 in lines:
        score += sum(c.isalnum() for c in text)
        if _NICK_TOKEN_RE.search(text):
            score += 15.0
    return score


def _best_kf_lines(kf_bgr: np.ndarray) -> List[Tuple[str, int, int]]:
    """Run line segmentation on several channels, return the best-scoring set."""
    best, best_q = [], -1.0
    for ch, inv in _KF_CHANNELS:
        lines = ocr_lines(kf_bgr, channel=ch, invert=inv)
        q = _lines_quality(lines)
        if q > best_q:
            best, best_q = lines, q
    return best


def _killfeed_rows(kf_bgr: np.ndarray, frame_h: int) -> List[Tuple[str, int, int]]:
    """
    Killfeed entries as (line_text, y1, y2). The TEXT comes straight from the
    best-channel line segmentation (re-OCRing a narrow half-crop reads markedly
    worse), and the y-band is padded for icon
    matching, clamped at the midpoint between neighbours so rows never merge.
    Falls back to projection bands + per-row OCR if segmentation finds nothing.
    """
    h = kf_bgr.shape[0]
    raw = [(t, y1, y2) for t, y1, y2 in _best_kf_lines(kf_bgr) if y2 > y1]
    if not raw:
        return [(ocr_nick(kf_bgr[y1:y2]), y1, y2)
                for y1, y2 in _detect_rows(kf_bgr, frame_h)][:config.KILLFEED_MAX_ROWS]

    ys = [(y1, y2) for _t, y1, y2 in raw]
    rows: List[Tuple[str, int, int]] = []
    for i, (text, y1, y2) in enumerate(raw):
        pad = max(2, int(0.30 * (y2 - y1)))
        top, bot = y1 - pad, y2 + pad
        if i > 0:                                    # don't cross into row above
            top = max(top, (ys[i - 1][1] + y1) // 2)
        if i < len(raw) - 1:                         # don't cross into row below
            bot = min(bot, (y2 + ys[i + 1][0]) // 2)
        rows.append((text, max(0, top), min(h, bot)))
    return rows[:config.KILLFEED_MAX_ROWS]


def _split_nicks(text: str) -> Tuple[str, str]:
    """
    Killer (first nick-like token) and victim (last nick-like token) from a
    killfeed line. Killer is always left, victim right. Junk (lone digits,
    1-2 char fragments, marker words) is dropped. Returns ('', '') if <2 nicks.
    """
    toks = re.findall(r"[A-Za-z0-9_]+\.[A-Za-z0-9]{1,5}|[A-Za-z0-9_]{2,}", text)
    cand = [t for t in toks
            if len(re.sub(r"\W", "", t)) >= 3 and not t.isdigit()
            and not _is_non_nick(t)]
    if len(cand) < 2:
        return "", ""
    return cand[0], cand[-1]


def _plausible_nick(text: str, known_names: List[str]) -> bool:
    """
    True if *text* could be a real nickname (not OCR noise). Real R6M nicks are
    4+ chars; we also accept a shorter string that clearly matches a roster nick.
    This rejects 1-3 char OCR noise that would otherwise count as a victim.
    """
    alnum = re.sub(r"[^A-Za-z0-9]", "", text)
    if len(set(alnum.lower())) < 2:          # '', 'c', 'nn', 'eee' → not a nick
        return False
    if len(alnum) >= 4:
        return True
    if known_names:
        base = text.split(".")[0] if "." in text else text
        _, fs = fuzzy_match(base, [n.split(".")[0] for n in known_names], min_score=0)
        return fs >= 80
    return False


def _norm_nick(s: str) -> str:
    """Lowercased alphanumerics only — for comparing nicks regardless of the
    dot/tag position (our 'Name.Tag' vs enemy 'Tag.Name') and dropped dots."""
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()


def _resolve_side(text: str, our_names: List[str], enemy_names: List[str],
                  min_score: int = 62) -> Tuple[Optional[str], Optional[str]]:
    """
    Snap an OCR'd name to the nearest KNOWN nick across both rosters and return
    (canonical_nick, team) where team is 'ours' / 'enemy', or (None, None) if it
    doesn't match anyone (→ OCR garbage, reject). Canonical nicks make dedup
    stable (the same enemy always maps to one string) and classification exact.

    Matches the WHOLE nick (alnum-normalized) with fuzz.ratio — NOT WRatio (whose
    partial-ratio rewards 2-char junk like 'ei' against 'SeiKo'), and NOT the part
    before the dot ('ACND.Sway'/'ACND.Gav' share the 'ACND' clan tag). A length
    floor drops 1-3 char noise outright.
    """
    norm = _norm_nick(text)
    if len(norm) < 4:
        return None, None
    best_nick, best_team, best_score = None, None, -1.0
    for team, names in (("ours", our_names or []), ("enemy", enemy_names or [])):
        for n in names:
            sc = fuzz.ratio(norm, _norm_nick(n))
            if sc > best_score:
                best_nick, best_team, best_score = n, team, sc
    if best_score >= min_score:
        return best_nick, best_team
    return None, None


def _pick_nick(cands: List[str], known_names: List[str]) -> str:
    """
    Choose the best reading of one side from several candidates (whole-line token
    vs per-half OCR). Prefers the candidate that most resembles a roster nick —
    OCR garbles names differently in each pass, so the best candidate wins.
    """
    cands = [c.strip() for c in cands if c and c.strip()]
    if not cands:
        return ""
    bases = [n.split(".")[0] for n in known_names] if known_names else []

    def score(c: str) -> float:
        s = float(sum(ch.isalnum() for ch in c))
        if _NICK_TOKEN_RE.search(c):
            s += 20.0
        if bases:
            base = c.split(".")[0] if "." in c else c
            _, fs = fuzzy_match(base, bases, min_score=0)
            s += fs                      # roster resemblance dominates
        return s

    return max(cands, key=score)


# ---------------------------------------------------------------------------
# Public reader
# ---------------------------------------------------------------------------

def read_killfeed(img_bgr, profile, known_names: List[str],
                  enemy_names: Optional[List[str]] = None) -> List[KillEvent]:
    kf = crop_named(img_bgr, profile, "killfeed")
    if kf is None or kf.size == 0:
        return []
    # When the enemy roster is known, every name is snapped to one of the 10 known
    # nicks and anything else is rejected — the most reliable mode.
    all_names = list(known_names) + list(enemy_names or [])

    kill_tmpls = _loaded(config.TEMPLATE_KILL_ICONS)
    dbno_tmpls = _loaded(config.TEMPLATE_DBNO_ICONS)
    thr = config.TEMPLATE_MATCH_THRESHOLD

    events: List[KillEvent] = []
    for text, y1, y2 in _killfeed_rows(kf, img_bgr.shape[0]):
        if not text.strip():
            continue
        row = kf[y1:y2]
        if row.size == 0:
            continue
        rh = y2 - y1
        rw = row.shape[1]
        row_gray = cv2.cvtColor(row, cv2.COLOR_BGR2GRAY)

        # Icon match constrained to the LEFT ~65% of the row: the kill/dbno icon
        # sits between the two names, while the right end holds the victim and the
        # operator portrait — which otherwise produce false template matches (and
        # a wrong killer/victim split).
        left_w = max(8, int(0.65 * rw))
        left_gray = row_gray[:, :left_w]
        dbno_score, dbno_x = _best_match(left_gray, dbno_tmpls, rh)
        kill_score, kill_x = _best_match(left_gray, kill_tmpls, rh)

        # Read each side from the BEST of: the whole-line token, and a per-half
        # OCR (ocr_nick tries both polarities). Per-half is essential because the
        # local player's name sits in a highlighted WHITE box (dark-on-light) while
        # the other name is light-on-colour — no single channel reads both.
        icon_x = kill_x or dbno_x or int(0.35 * rw)
        icon_x = max(12, min(rw - 12, icon_x))
        pad = max(2, int(0.02 * rw))
        killer_half = row[:, :max(1, icon_x - pad)]
        victim_half = row[:, min(rw - 1, icon_x + pad):]
        band_k, band_v = _split_nicks(text)
        killer_text = _pick_nick([band_k, ocr_nick(killer_half, known_names)],
                                 all_names)
        victim_text = _pick_nick([band_v, ocr_nick(victim_half, known_names)],
                                 all_names)
        enemy_mode = bool(enemy_names)

        def _emit_first_blood():
            if enemy_mode:
                k_nick, k_team = _resolve_side(killer_text, known_names, enemy_names)
                if k_nick is None:
                    return
                events.append(KillEvent(
                    killer=k_nick, victim="", killer_tag="", victim_tag="",
                    our_kill=False, our_death=False, is_first_blood=True,
                    fb_killer_ours=(k_team == "ours"), raw=text,
                ))
                return
            # Require a plausible killer nick — never manufacture a first blood
            # from a noisy/empty row.
            if len(re.sub(r"\W", "", killer_text)) < 3:
                return
            k_base, k_tag, k_ours = _parse_side(killer_text, known_names)
            if k_base:
                events.append(KillEvent(
                    killer=f"{k_base}.{k_tag}" if k_tag else k_base,
                    victim="", killer_tag=k_tag, victim_tag="",
                    our_kill=False, our_death=False,
                    is_first_blood=True, fb_killer_ours=k_ours, raw=text,
                ))

        # First Blood marker (opening kill): "<killer> [icon] First Blood!".
        # The RIGHT side is the marker text, NOT a nick — nobody is named
        # "First Blood!". Record the opening-killer only (no victim).
        if _is_first_blood(text) or (victim_text and _is_first_blood(victim_text)):
            _emit_first_blood()
            continue

        # Knock (DBNO): skip if the down-arrow icon wins
        if dbno_score >= thr and dbno_score > kill_score:
            continue
        # Confirmed-kills-only: require the kill icon (crosshair/skull) to match
        if config.KILLFEED_REQUIRE_KILL_ICON and not (
                kill_score >= thr and kill_score >= dbno_score):
            continue

        # A real kill needs a real victim. If the right side is a marker word
        # ("First Blood!"/"Headshot"), it's a first-blood marker; if it's empty,
        # the row is noise — skip it (do NOT invent a victim OR a first blood).
        if not victim_text:
            continue
        if _is_non_nick(victim_text):
            _emit_first_blood()
            continue

        if enemy_mode:
            # Snap both names to a known roster nick. Canonical nicks make dedup
            # stable (one kill read across many frames counts once).
            k_nick, k_team = _resolve_side(killer_text, known_names, enemy_names)
            v_nick, v_team = _resolve_side(victim_text, known_names, enemy_names)

            # OUR DEATH: our player is the victim. Record it even if the killer
            # is unreadable — a missed death wrongly flips KOST to 'survived'.
            if v_team == "ours" and k_team != "ours":
                events.append(KillEvent(
                    killer=k_nick or killer_text or "?", victim=v_nick,
                    killer_tag="", victim_tag="",
                    our_kill=False, our_death=True,
                    is_first_blood=False, fb_killer_ours=False, raw=text,
                ))
                continue
            # OUR KILL: our player killed a known enemy (both must resolve, so a
            # garbled victim can't inflate kill counts).
            if k_team == "ours" and v_team == "enemy":
                events.append(KillEvent(
                    killer=k_nick, victim=v_nick,
                    killer_tag="", victim_tag="",
                    our_kill=True, our_death=False,
                    is_first_blood=False, fb_killer_ours=False, raw=text,
                ))
                continue
            # Neither side is ours (enemy-vs-enemy) or unresolved → not our stat.
            continue

        # Fallback (no enemy roster supplied): length-plausibility + tag/lineup snap.
        if not _plausible_nick(killer_text, known_names) \
                or not _plausible_nick(victim_text, known_names):
            continue
        k_base, k_tag, k_ours = _parse_side(killer_text, known_names)
        v_base, v_tag, v_ours = _parse_side(victim_text, known_names)
        if not k_base or not v_base:
            continue

        events.append(KillEvent(
            killer=f"{k_base}.{k_tag}" if k_tag else k_base,
            victim=f"{v_base}.{v_tag}" if v_tag else v_base,
            killer_tag=k_tag, victim_tag=v_tag,
            our_kill=k_ours and not v_ours,
            our_death=v_ours and not k_ours,
            is_first_blood=False, fb_killer_ours=False, raw=text,
        ))

    return events


def debug_rows(img_bgr, profile, known_names: List[str]) -> List[dict]:
    """
    Per-row diagnostics for tuning: kill_score, dbno_score, OCR text, decision.
    Lets you verify the icon templates / threshold in test_ocr.
    """
    kf = crop_named(img_bgr, profile, "killfeed")
    if kf is None or kf.size == 0:
        return []
    kill_tmpls = _loaded(config.TEMPLATE_KILL_ICONS)
    dbno_tmpls = _loaded(config.TEMPLATE_DBNO_ICONS)
    thr = config.TEMPLATE_MATCH_THRESHOLD

    out = []
    for idx, (text, y1, y2) in enumerate(_killfeed_rows(kf, img_bgr.shape[0])):
        row = kf[y1:y2]
        if row.size == 0:
            continue
        rh = y2 - y1
        g = cv2.cvtColor(row, cv2.COLOR_BGR2GRAY)
        ks, _ = _best_match(g, kill_tmpls, rh)
        ds, _ = _best_match(g, dbno_tmpls, rh)
        _, victim_text = _split_nicks(text)
        if _is_first_blood(text) or _is_first_blood(victim_text):
            decision = "FIRST BLOOD (killer only)"
        elif ds >= thr and ds > ks:
            decision = "DBNO (skip)"
        elif ks >= thr and ks >= ds:
            decision = "KILL"
        else:
            decision = "no-icon (skip)" if config.KILLFEED_REQUIRE_KILL_ICON else "KILL?"
        out.append({"row": idx, "y": f"{y1}-{y2}", "kill": round(ks, 2),
                    "dbno": round(ds, 2), "decision": decision, "text": text})
    return out
