#!/usr/bin/env python3
"""
R6M VOD Analyzer — LOCAL pipeline (Tesseract + OpenCV, no paid APIs).

Input: folder of screenshots, a local video file (e.g. raw phone recording
sent via Telegram as a FILE), or a YouTube URL + timecodes.
Output: console report (frame timeline + round summary) + auto-saved .txt/.json
copies in out/<profile>/reports/.

Architecture (fast):
  Stage 1 — per-frame OCR/vision extraction, PARALLEL (ThreadPool: each
            Tesseract call is a separate subprocess, so threads scale well).
  Stage 2 — sequential reduce: round segmentation, dedup, trades, OK/OD.
Stage 2 logic is order-dependent and stays sequential; stage 1 is
per-frame independent.
"""

import glob
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import click
import cv2
import yaml
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.prompt import Confirm, Prompt

import config
from regions_loader import load_profile
from phase_detector import (
    detect_phase, get_banner_texts, round_result, read_round_number,
    read_side_hint, read_score, objective_plant_state,
    PREP, ACTION, ROUND_END,
)
from killfeed_reader import read_killfeed, templates_available
from timer_reader import read_timer
from plant_reader import read_plant_event
from dedup import KillfeedDeduper
from console_report import (
    print_frame_timeline, print_round_summary, print_warnings,
    save_report, console,
)
from video_processor import download_segment, extract_frames, sec_to_ts


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _frames_from_folder(folder: str) -> list:
    exts = ("*.png", "*.jpg", "*.jpeg", "*.bmp")
    paths = []
    for e in exts:
        paths.extend(glob.glob(os.path.join(folder, e)))
    return sorted(paths)


def _expected_side(start_side: str, round_num: int) -> str:
    """Sides alternate every round in R6M Mobile."""
    flip = (round_num - 1) % 2 == 1
    if not flip:
        return start_side
    return "DEF" if start_side == "ATK" else "ATK"


def _list_profiles(yaml_path: str):
    with open(yaml_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "profiles" in cfg:
        return list(cfg["profiles"].keys()), cfg.get("default_profile")
    return [cfg.get("profile", "default")], None


def _choose_profile(cli_profile):
    """Resolve the profile: CLI flag, single profile, or interactive pick."""
    if cli_profile:
        return cli_profile
    names, default = _list_profiles(config.REGIONS_YAML)
    if len(names) == 1:
        console.print(f"HUD profile: [bold]{names[0]}[/bold] (only one available)")
        return names[0]
    console.print("\n[bold]Available HUD profiles:[/bold]")
    for i, n in enumerate(names, 1):
        mark = " [dim](default)[/dim]" if n == default else ""
        console.print(f"  {i}. {n}{mark}")
    default_idx = str(names.index(default) + 1) if default in names else "1"
    choice = Prompt.ask("Select profile",
                        choices=[str(i) for i in range(1, len(names) + 1)],
                        default=default_idx)
    return names[int(choice) - 1]


def _kill_kind(ev) -> str:
    if ev.our_kill:
        return "our_kill"
    if ev.our_death:
        return "our_death"
    return "other"


def _nick_eq(a: str, b: str) -> bool:
    """Fuzzy-compare two raw nicks (OCR-tolerant)."""
    if not a or not b:
        return False
    if a.lower() == b.lower():
        return True
    from rapidfuzz import fuzz
    return fuzz.WRatio(a, b) >= 85


# Per-match in-game-nick → role/column map, built from the lineup. Falls back to
# config.ROSTER_ALIASES for alternate spellings / bench players.
_NICK2ROLE: dict = {}


def _norm_nick(s: str) -> str:
    """Alnum-only, lowercased — so 'Mr.Bom.AB', 'MrBom.AB', 'mrbom' all match."""
    import re as _re
    return _re.sub(r"[^A-Za-z0-9]", "", s or "").lower()


def set_lineup(lineup: list):
    """lineup = 5 in-game nicks parallel to config.SHEET_PLAYERS. Keyed by the
    FULL normalized nick (dots/tag/case don't matter), with the pre-dot base as a
    fallback key."""
    _NICK2ROLE.clear()
    for nick, role in zip(lineup, config.SHEET_PLAYERS):
        _NICK2ROLE[_norm_nick(nick)] = role
        _NICK2ROLE.setdefault(nick.split(".")[0].lower(), role)


def _canonical(nick: str) -> str:
    """Map an in-game killfeed nick to its sheet role/column."""
    if not nick:
        return nick
    base = nick.split(".")[0]
    full = _norm_nick(nick)
    low = base.lower()
    if full in _NICK2ROLE:                      # full nick (preferred)
        return _NICK2ROLE[full]
    if low in _NICK2ROLE:                       # pre-dot base fallback
        return _NICK2ROLE[low]
    for k, v in config.ROSTER_ALIASES.items():  # extra aliases (exact / ci)
        if k.lower() == low:
            return v
    # fuzzy against lineup nicks, then aliases
    from rapidfuzz import process, fuzz
    if _NICK2ROLE:
        m = process.extractOne(low, list(_NICK2ROLE.keys()), scorer=fuzz.WRatio)
        if m and m[1] >= config.FUZZY_MIN_SCORE:
            return _NICK2ROLE[m[0]]
    if config.ROSTER_ALIASES:
        m = process.extractOne(base, list(config.ROSTER_ALIASES.keys()),
                               scorer=fuzz.WRatio)
        if m and m[1] >= config.FUZZY_MIN_SCORE:
            return config.ROSTER_ALIASES[m[0]]
    return base


def _compute_trades(r: dict, warnings: list):
    """
    A teammate's death is traded if another teammate kills the SAME enemy within
    TRADE_WINDOW_SEC after the death. If a teammate kills a DIFFERENT enemy in
    the window → gray area, flagged with timing.
    """
    log = r["kill_log"]
    window = config.TRADE_WINDOW_SEC

    for d in log:
        if d["kind"] != "our_death":
            continue
        t_d   = d["t"]
        enemy = d["killer"]
        victim_team = _canonical(d["victim"])

        cands = [k for k in log
                 if k["kind"] == "our_kill" and t_d < k["t"] <= t_d + window]
        clean = next((k for k in cands if _nick_eq(k["victim"], enemy)), None)

        if clean:
            r["traded"][victim_team] = _canonical(clean["killer"])
        elif cands:
            k = cands[0]
            note = (
                f"Round {r['round']}: {victim_team} died to {enemy} @{sec_to_ts(t_d)}; "
                f"{_canonical(k['killer'])} killed {k['victim']} @{sec_to_ts(k['t'])} "
                f"(+{k['t'] - t_d}s) — different enemy → GRAY AREA, verify trade."
            )
            r["trade_flags"].append(note)
            warnings.append(note)


def _compute_kost(rounds: list, roster: list):
    """
    KOST per player per round (0 or 1).
    1 if the player did ANY of: got a Kill, planted the Objective, Survived,
    or their death was Traded. 0 only if they died, got no kill, didn't plant,
    and weren't traded.

    Caveat: 'survived' = no death detected for them. A missed death (OCR) would
    wrongly count as survived → KOST 1.
    """
    for r in rounds:
        plant_by = r.get("plant_by") if r.get("plant") else None  # already a role
        kost = {}
        for p in roster:
            k = r["our_kills"].get(p, 0) > 0          # Kill
            o = (plant_by == p)                        # Objective (plant)
            s = p not in r["our_deaths"]               # Survive
            t = p in r.get("traded", {})               # Traded
            kost[p] = 1 if (k or o or s or t) else 0
        r["kost"] = kost


# ---------------------------------------------------------------------------
# Stage 1 — per-frame extraction (PARALLEL, order-independent)
# ---------------------------------------------------------------------------

def _extract_frame_data(idx: int, path: str, profile: dict, known_names: list,
                        enemy_names: list = None):
    """
    All OCR/vision work for one frame. Pure per-frame: no shared state, so it
    runs safely in a thread pool. Banner regions are OCR'd exactly once.
    """
    img = cv2.imread(path)
    if img is None:
        return idx, None

    texts = get_banner_texts(img, profile)          # one OCR pass for 3 banners
    phase = detect_phase(img, profile, texts)
    data = {"phase": phase}

    if phase == ROUND_END:
        data["result"] = round_result(img, profile, texts)
        data["round_num"] = read_round_number(img, profile)
        # End-of-round / elimination kills linger on the killfeed UNDER the result
        # banner (top-left is not covered) — read them so a team-wipe round doesn't
        # lose its final deaths (action frames may end before those kills land).
        data["kills"] = read_killfeed(img, profile, known_names, enemy_names)
    else:
        # action / prep / op_pick / unknown — the objective banner OCR is flaky and
        # often mislabels a real action frame as op_pick, so we read the live HUD
        # AND the killfeed on every non-round-end frame. Reading the killfeed on a
        # true operator-select screen is harmless (it's empty there); the time-window
        # deduper stops a kill from being counted twice across the boundary.
        data["score"] = read_score(img, profile)
        data["side_hint"] = read_side_hint(img, profile, texts)
        data["obj_plant"] = objective_plant_state(img, profile, texts)
        data["timer"] = read_timer(img, profile)
        data["plant"] = read_plant_event(img, profile,
                                         raw=texts.get("plant_banner"))
        data["kills"] = read_killfeed(img, profile, known_names, enemy_names)

    return idx, data


def _run_stage1(frame_paths, profile, known_names, workers, progress_cb=None,
                enemy_names=None):
    """
    Parallel per-frame extraction. Returns ordered list.
    *progress_cb(done, total)* — if given, used instead of the Rich bar
    (e.g. the GUI passes its own callback).
    """
    results = [None] * len(frame_paths)
    total = len(frame_paths)

    if progress_cb is not None:
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_extract_frame_data, i, p, profile, known_names, enemy_names)
                for i, p in enumerate(frame_paths)
            ]
            for fut in as_completed(futures):
                idx, data = fut.result()
                results[idx] = data
                done += 1
                progress_cb(done, total)
        return results

    progress = Progress(
        TextColumn("[bold blue]Analyzing frames"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    with progress:
        task = progress.add_task("ocr", total=total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_extract_frame_data, i, p, profile, known_names, enemy_names)
                for i, p in enumerate(frame_paths)
            ]
            for fut in as_completed(futures):
                idx, data = fut.result()
                results[idx] = data
                progress.advance(task)
    return results


# ---------------------------------------------------------------------------
# Stage 2 — sequential reduce (round segmentation, dedup, trades)
# ---------------------------------------------------------------------------

def _reduce(frame_data, frame_paths, interval, start_side, warnings):
    timeline = []
    rounds = []
    deduper = KillfeedDeduper()
    round_counter = 0
    current = None
    ended = False

    def open_round():
        nonlocal current, round_counter
        round_counter += 1
        current = {
            "round": round_counter,
            "side": _expected_side(start_side, round_counter),
            "score": None,
            "score_votes": [],
            "result": "?",
            "time": "",
            "operators": [None] * 5,
            "kills": [],
            "our_kills": {},
            "our_deaths": {},
            "kill_log": [],
            "ok_od": None,
            "ok_od_who": None,
            "traded": {},
            "trade_flags": [],
            "plant": False,
            "plant_by": None,
            "plant_started": False,
        }
        rounds.append(current)
        # NOTE: the deduper is intentionally NOT reset per round. A kill lingers
        # on the killfeed across the round boundary; the time-window dedup keeps
        # it attributed to the round it FIRST appeared in (and still allows the
        # same killer→victim pair to recur in a much later round).

    def process_kills(cur, raw_events, t_sec, timer):
        """Fold killfeed events into a round (dedup, our_kills/deaths, OK/OD).
        Returns the (killer, victim, kind) list for the timeline."""
        out = []
        for ev in deduper.filter(raw_events or [], t_sec):
            if ev.is_first_blood:
                if cur["ok_od"] is None:
                    cur["ok_od"] = "OK" if ev.fb_killer_ours else "OD"
                    if ev.fb_killer_ours:
                        cur["ok_od_who"] = _canonical(ev.killer)
                        cur["time"] = timer or cur["time"]
                        cur["time_locked"] = True
                continue

            kind = _kill_kind(ev)
            out.append((ev.killer, ev.victim, kind))
            cur["kills"].append((ev.killer, ev.victim, kind))
            cur["kill_log"].append(
                {"t": t_sec, "killer": ev.killer, "victim": ev.victim, "kind": kind}
            )

            if ev.our_kill:
                name = _canonical(ev.killer)
                cur["our_kills"][name] = cur["our_kills"].get(name, 0) + 1
            elif ev.our_death:
                name = _canonical(ev.victim)
                cur["our_deaths"][name] = ev.killer

            # Opening kill/death (OK/OD). The TYPE may already be fixed by the
            # First Blood marker; fill in WHO when unknown (order-independent).
            if kind == "our_kill" and cur["ok_od"] in (None, "OK") \
                    and not cur["ok_od_who"]:
                cur["ok_od"] = "OK"
                cur["ok_od_who"] = _canonical(ev.killer)
                cur["time"] = timer or cur["time"]
                cur["time_locked"] = True
            elif kind == "our_death" and cur["ok_od"] in (None, "OD") \
                    and not cur["ok_od_who"]:
                cur["ok_od"] = "OD"
                cur["ok_od_who"] = _canonical(ev.victim)
                cur["time"] = timer or cur["time"]
                cur["time_locked"] = True
        return out

    for idx, data in enumerate(frame_data):
        if data is None:
            warnings.append(f"Could not read frame: {frame_paths[idx]}")
            continue

        t_sec = idx * interval
        phase = data["phase"]

        # ---- ROUND-END ----
        if phase == ROUND_END:
            kills_for_frame = []
            if current is not None:
                if current["result"] == "?":
                    current["result"] = data.get("result", "?")
                # Harvest end-of-round / elimination kills into the round that
                # just ended (their killfeed entries linger under the banner).
                kills_for_frame = process_kills(current, data.get("kills"),
                                                t_sec, None)
            ended = True
            timeline.append({"time": sec_to_ts(t_sec), "phase": phase,
                             "timer": None, "plant": False,
                             "plant_event": None, "kills": kills_for_frame})
            continue

        if current is None or ended:
            open_round()
            ended = False

        # Score (HUD = completed rounds). It's read per-frame and OCR is noisy, so
        # we only COLLECT votes here and majority-vote at the end — we do NOT use
        # it to set the round number (sequential round_counter is the truth).
        sc = data.get("score")
        if sc:
            our, enemy = sc
            if 0 <= our <= 15 and 0 <= enemy <= 15:
                current["score_votes"].append((our, enemy))

        # Side from the objective banner (any phase) overrides the alternation
        # guess — catches a wrong start-side entry.
        sh = data.get("side_hint")
        if sh:
            current["side"] = sh
            current["side_locked"] = True

        plant = False
        plant_event = None

        # Non-round-end frames (action / prep / op_pick) all carry live HUD data —
        # the objective-banner phase label is too flaky to gate killfeed reading on,
        # so we process the killfeed/timer/plant on every such frame.
        timer = data.get("timer")
        # Track the round clock, but FREEZE it at the OK/OD moment (below): the
        # 'Time' column is the timer when the opening kill/death happened.
        if timer and not current.get("time_locked"):
            current["time"] = timer

        pe = data.get("plant") or {"state": None}
        if pe["state"] == "start" and pe["ours"]:
            current["plant_started"] = True
            if pe["planter"]:
                current["plant_by"] = _canonical(pe["planter"])
            plant_event = f"START by {pe['planter']}"
            plant = True
        elif pe["state"] == "confirm" and pe["ours"]:
            current["plant"] = True
            current["plant_started"] = True
            plant_event = "CONFIRM"
            plant = True

        # Persistent plant confirm from the objective banner ("defend the
        # defuser" while we're ATK) — robust even if the 3s notification was missed.
        if data.get("obj_plant") == "ours":
            current["plant"] = True
            current["plant_started"] = True
            plant = True
            if not plant_event:
                plant_event = "PLANTED (objective)"

        kills_for_frame = process_kills(current, data.get("kills"),
                                        t_sec, timer)

        timeline.append({"time": sec_to_ts(t_sec), "phase": phase,
                         "timer": timer, "plant": plant,
                         "plant_event": plant_event, "kills": kills_for_frame})

    # --- finalize rounds -----------------------------------------------------
    # Drop spurious "fragment" rounds: opened on a transition / post-game screen
    # but never saw real action (no kills, no score, no opening, no plant). Then
    # renumber sequentially — the round number is the reliable signal, NOT the
    # noisy per-frame score OCR.
    def _is_fragment(r):
        return (not r["kills"] and not r["score_votes"]
                and r["ok_od"] is None and not r["plant"]
                and not r["plant_started"])

    rounds[:] = [r for r in rounds if not _is_fragment(r)]

    from collections import Counter
    won = lost = 0
    for i, r in enumerate(rounds, 1):
        r["round"] = i
        if not r.get("side_locked"):
            r["side"] = _expected_side(start_side, i)
        # Score = completed rounds GOING INTO this round, computed from results
        # (the round-end WON/DEFEAT banner is far more reliable than score-digit
        # OCR). The noisy per-frame score reading is used only to cross-check.
        r["score"] = (won, lost)
        if r["result"] == "WIN":
            won += 1
        elif r["result"] == "LOSS":
            lost += 1
        if r["score_votes"]:
            ocr = Counter(r["score_votes"]).most_common(1)[0][0]
            if ocr != r["score"]:
                warnings.append(
                    f"Round {i}: score OCR {ocr[0]}-{ocr[1]} ≠ computed "
                    f"{r['score'][0]}-{r['score'][1]} — verify the round result."
                )

    for r in rounds:
        _compute_trades(r, warnings)

    for r in rounds:
        if r.get("plant_started") and not r.get("plant"):
            warnings.append(
                f"Round {r['round']}: plant STARTED by "
                f"{r.get('plant_by') or '?'} but no CONFIRM seen "
                f"→ likely defused/interrupted (verify)."
            )

    return timeline, rounds


def _dump_debug(frame_paths, frame_data, interval, debug_dir):
    """
    Save every analyzed frame + a TSV of what each frame read, so you can open a
    frame and see exactly what the OCR produced for it (phase/score/kills/etc.).
    """
    import shutil
    frames_dir = os.path.join(debug_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    lines = ["idx\ttime\tframe\tphase\tscore\tside\ttimer\tobj_plant\tplant\tkills"]
    for i, path in enumerate(frame_paths):
        t = sec_to_ts(i * interval)
        fname = f"{i:04d}_{t.replace(':', '-')}.jpg"
        try:
            shutil.copy(path, os.path.join(frames_dir, fname))
        except Exception:
            pass
        d = frame_data[i] or {}
        score = d.get("score")
        kills = d.get("kills") or []
        kstr = "; ".join(f"{e.killer}->{e.victim}" for e in kills)
        pe = d.get("plant") or {}
        lines.append("\t".join(str(x) for x in [
            i, t, fname, d.get("phase", ""),
            f"{score[0]}-{score[1]}" if score else "",
            d.get("side_hint", "") or "", d.get("timer", "") or "",
            d.get("obj_plant", "") or "", pe.get("state", "") or "", kstr,
        ]))
    with open(os.path.join(debug_dir, "frames_debug.tsv"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def analyze_frames(frame_paths, profile, interval, start_side, lineup,
                   workers: int = None, progress_cb=None, debug_dir=None,
                   enemy=None):
    """
    Stage 1 (parallel) + Stage 2 (sequential).
    *lineup* = 5 in-game nicks parallel to config.SHEET_PLAYERS (this match's
    roster, for nick→role mapping and killfeed OCR cleanup).
    *debug_dir* — if set, dump every frame + a per-frame OCR log there.
    """
    if workers is None:
        workers = min(8, os.cpu_count() or 4)
    set_lineup(lineup)
    warnings = []
    frame_data = _run_stage1(frame_paths, profile, lineup, workers,
                             progress_cb=progress_cb, enemy_names=enemy)
    if debug_dir:
        _dump_debug(frame_paths, frame_data, interval, debug_dir)
    timeline, rounds = _reduce(frame_data, frame_paths, interval,
                               start_side, warnings)
    _compute_kost(rounds, config.SHEET_PLAYERS)
    return timeline, rounds, warnings


# ---------------------------------------------------------------------------
# Report saving
# ---------------------------------------------------------------------------

def _save_outputs(profile_name: str, rounds: list):
    """Save the console report (.txt) and raw rounds (.json). Returns paths."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(config.OUT_DIR) / profile_name / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    txt_path = out_dir / f"report_{stamp}.txt"
    json_path = out_dir / f"rounds_{stamp}.json"

    save_report(str(txt_path))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rounds, f, ensure_ascii=False, indent=2, default=str)

    return txt_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--frames", "frames_folder", default=None,
              help="Folder of screenshots (chronological by filename)")
@click.option("--video", "video_path", default=None,
              help="Local video file (e.g. raw phone recording sent via Telegram)")
@click.option("--url", default=None, help="YouTube VOD URL")
@click.option("--start", default=None, help="Segment start (HH:MM:SS) — for --url/--video")
@click.option("--end", default=None, help="Segment end (HH:MM:SS) — for --url/--video")
@click.option("--profile", default=None, help="HUD profile name from regions.yaml")
@click.option("--interval", default=config.FRAME_INTERVAL_SEC, show_default=True,
              help="Frame interval (seconds)")
@click.option("--start-side", default=None, type=click.Choice(["ATK", "DEF"]),
              help="Side of round 1 (asked if omitted)")
@click.option("--vs", "vs", default=None, help="Opponent tag (asked if omitted)")
@click.option("--map", "map_name", default=None, help="Map name (asked if omitted)")
@click.option("--date", default=None, help="Match date e.g. 05.04.2026 (asked if omitted)")
@click.option("--lineup", default=None,
              help="5 in-game nicks for roles "
                   f"({', '.join(config.SHEET_PLAYERS)}), comma-separated. "
                   "Blank = core roster.")
@click.option("--enemy", default=None,
              help="Enemy roster: in-game nicks, comma-separated. Optional but "
                   "recommended — names are snapped to the 10 known nicks, "
                   "rejecting OCR garbage and fixing kill counts.")
@click.option("--workers", default=None, type=int,
              help="Parallel OCR workers (default: min(8, CPU cores))")
@click.option("--debug", is_flag=True,
              help="Save analyzed frames + a per-frame OCR log to out/<profile>/debug_*")
def main(frames_folder, video_path, url, start, end, profile, interval,
         start_side, vs, map_name, date, lineup, enemy, workers, debug):
    """Analyze an R6M VOD locally and print results + a paste-ready TSV."""

    console.print("[bold cyan]R6M VOD Analyzer — local (Tesseract + OpenCV)[/bold cyan]\n")

    # ── HUD profile (interactive if not given) ───────────────────────────
    profile = _choose_profile(profile)
    try:
        profile_name, prof = load_profile(config.REGIONS_YAML, profile)
    except (FileNotFoundError, KeyError) as e:
        console.print(f"[red]Profile error: {e}[/red]")
        sys.exit(1)

    if not templates_available():
        console.print(
            "[yellow]⚠ Killfeed templates missing — kills won't be read. "
            "Create them:\n"
            "   python make_template.py <frame> --name icon_kill\n"
            "   python make_template.py <frame> --name icon_kill2\n"
            "   python make_template.py <frame> --name icon_dbno[/yellow]"
        )

    # ── Match info & roster (interactive if not given) ───────────────────
    from datetime import date as _date
    if not start_side:
        start_side = Prompt.ask("Starting side of round 1",
                                choices=["ATK", "DEF"], default="ATK")
    if not vs:
        vs = Prompt.ask("Opponent (VS)")
    if not map_name:
        map_name = Prompt.ask("Map")
    if not date:
        date = Prompt.ask("Date", default=_date.today().strftime("%d.%m.%Y"))

    # Lineup: 5 in-game nicks parallel to SHEET_PLAYERS (roles). Default core.
    default_lineup = list(config.ROSTER_NICKS)
    if lineup:
        lineup_list = [x.strip() for x in lineup.split(",")]
    else:
        console.print(f"[dim]Roles: {', '.join(config.SHEET_PLAYERS)}[/dim]")
        ans = Prompt.ask("Lineup (5 nicks comma-separated, blank = core roster)",
                         default=", ".join(default_lineup))
        lineup_list = [x.strip() for x in ans.split(",")]
    # pad/trim to 5
    lineup_list = (lineup_list + default_lineup)[:len(config.SHEET_PLAYERS)]
    enemy_list = [x.strip() for x in enemy.split(",") if x.strip()] if enemy else None

    # ── Obtain frames ────────────────────────────────────────────────────
    temp_dir = None
    source_desc = ""
    if frames_folder:
        frame_paths = _frames_from_folder(frames_folder)
        if not frame_paths:
            console.print(f"[red]No images found in {frames_folder}[/red]")
            sys.exit(1)
        source_desc = f"folder: {frames_folder}"
    elif video_path:
        if not os.path.exists(video_path):
            console.print(f"[red]Video file not found: {video_path}[/red]")
            sys.exit(1)
        # The recording may start at lobby creation, not at the match — offer to trim.
        if not (start or end):
            console.print("[dim]The recording may include lobby/prep before the match.[/dim]")
            if Confirm.ask("Trim to a time range?", default=True):
                start = Prompt.ask("  Start (HH:MM:SS, blank = beginning)", default="") or None
                end   = Prompt.ask("  End (HH:MM:SS, blank = end)", default="") or None
        source_desc = (f"video: {os.path.basename(video_path)}"
                       f"  [{start or 'start'} → {end or 'end'}]")
    elif url:
        if not (start and end):
            console.print("Specify the segment to download (HH:MM:SS):")
            start = start or Prompt.ask("  Start")
            end   = end or Prompt.ask("  End")
        source_desc = f"YouTube: {url}  [{start} → {end}]"
    else:
        console.print("[red]Provide --frames <folder>, --video <file>, or --url <YouTube>[/red]")
        sys.exit(1)

    n_workers = workers or min(8, os.cpu_count() or 4)

    # ── Settings summary ─────────────────────────────────────────────────
    lineup_str = ", ".join(f"{role}={nick}"
                           for role, nick in zip(config.SHEET_PLAYERS, lineup_list))
    console.print(Panel.fit(
        f"[bold]Source:[/bold]    {source_desc}\n"
        f"[bold]Match:[/bold]     {date}  vs {vs}  on {map_name}\n"
        f"[bold]Profile:[/bold]   {profile_name}\n"
        f"[bold]Lineup:[/bold]    {lineup_str}\n"
        f"[bold]Side R1:[/bold]   {start_side}    "
        f"[bold]Interval:[/bold] {interval}s    "
        f"[bold]Workers:[/bold] {n_workers}",
        title="Run settings", border_style="cyan",
    ))

    # ── Extract frames (video/url sources) ──────────────────────────────
    if video_path:
        with console.status("[dim]Extracting frames with ffmpeg...[/dim]"):
            frame_paths = extract_frames(video_path, interval_sec=interval,
                                         start=start, end=end)
        console.print(f"Extracted {len(frame_paths)} frames")
    elif url:
        temp_dir = tempfile.mkdtemp(prefix="r6m_local_")
        seg_path = os.path.join(temp_dir, "segment.mp4")
        with console.status(f"[dim]Downloading {start} → {end}...[/dim]"):
            download_segment(url, start, end, seg_path)
        with console.status("[dim]Extracting frames with ffmpeg...[/dim]"):
            frame_paths = extract_frames(seg_path, interval_sec=interval)
        console.print(f"Extracted {len(frame_paths)} frames")

    # ── Analyze ──────────────────────────────────────────────────────────
    from datetime import datetime as _dt
    debug_dir = None
    if debug:
        debug_dir = os.path.join(config.OUT_DIR, profile_name,
                                 f"debug_{_dt.now().strftime('%Y%m%d_%H%M%S')}")
        console.print(f"[yellow]Debug: frames + per-frame OCR log → {debug_dir}[/yellow]")
    timeline, rounds, warnings = analyze_frames(
        frame_paths, prof, interval, start_side, lineup_list,
        workers=n_workers, debug_dir=debug_dir, enemy=enemy_list,
    )

    # ── Output ───────────────────────────────────────────────────────────
    console.print()
    print_frame_timeline(timeline)
    console.print()
    print_round_summary(rounds)
    print_warnings(warnings)

    console.print(
        f"\n[green]Done.[/green] {len(rounds)} rounds, "
        f"{sum(len(r['kills']) for r in rounds)} kills detected."
    )

    # ── Auto-save report + paste-ready TSV ───────────────────────────────
    import sheet_export
    match = {"date": date, "vs": vs, "map": map_name, "start_side": start_side}
    txt_path, json_path = _save_outputs(profile_name, rounds)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tsv_path = Path(config.OUT_DIR) / profile_name / "reports" / f"sheet_{stamp}.tsv"
    sheet_export.save_tsv(rounds, match, str(tsv_path))

    console.print(f"\n[bold green]Paste-ready TSV (open & copy, or paste from file):[/bold green]")
    console.print(f"[dim]{tsv_path}[/dim]\n")
    console.print(sheet_export.to_tsv(rounds, match))
    console.print(f"\n[dim]Report: {txt_path}   JSON: {json_path}[/dim]")

    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
