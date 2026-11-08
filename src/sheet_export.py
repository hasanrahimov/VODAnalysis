"""
Export rounds as a TSV (tab-separated) matching the PerRoundStat sheet layout,
so it can be pasted straight into Google Sheets (tabs → columns).

Auto-filled: Date, VS., Map, Round, Side, Result, Plant?, Who?(plant), OK/OD,
Who?(opening), Time, per-player Kills/Death?/Death Traded?/Who Traded?, KOST.
Left blank for manual entry: Site, Strat, Operators, 1vX.
"""

from typing import List, Dict

import config


def _players() -> List[str]:
    return config.SHEET_PLAYERS


def columns() -> List[str]:
    p = _players()
    cols = ["Date", "VS.", "Map", "Round", "Side", "Site", "Strat", "Result",
            "Plant?", "Who?", "OK/OD", "Who?", "Time", "1vX"]
    cols += [f"{x} Operator" for x in p]                 # operators grouped
    for x in p:
        cols += [f"{x} Kills", f"{x} Death?", "Death Traded?", "Who Traded?"]
    cols += [f"KOST {x}" for x in p]
    return cols


def _bool(b) -> str:
    return "TRUE" if b else "FALSE"


def build_row(rd: Dict, match: Dict) -> List[str]:
    p = _players()
    ok = rd.get("our_kills", {})
    od = rd.get("our_deaths", {})
    tr = rd.get("traded", {})
    kost = rd.get("kost", {})

    row = [
        match.get("date", ""),
        match.get("vs", ""),
        match.get("map", ""),
        rd.get("round", ""),
        rd.get("side", ""),
        "",                                              # Site (manual)
        "",                                              # Strat (manual)
        rd.get("result", ""),
        _bool(rd.get("plant", False)),
        rd.get("plant_by", "") if rd.get("plant") else "",
        rd.get("ok_od", "") or "",
        rd.get("ok_od_who", "") or "",
        rd.get("time", "") or "",
        "",                                              # 1vX (manual)
    ]
    row += ["" for _ in p]                               # operators (manual)
    for name in p:
        row += [ok.get(name, 0), _bool(name in od), _bool(name in tr), tr.get(name, "")]
    row += [kost.get(name, "") for name in p]
    return [str(c) for c in row]


def to_tsv(rounds: List[Dict], match: Dict, header: bool = True) -> str:
    lines = []
    if header:
        lines.append("\t".join(columns()))
    for rd in rounds:
        lines.append("\t".join(build_row(rd, match)))
    return "\n".join(lines)


def save_tsv(rounds: List[Dict], match: Dict, path: str, header: bool = True):
    with open(path, "w", encoding="utf-8") as f:
        f.write(to_tsv(rounds, match, header=header))
