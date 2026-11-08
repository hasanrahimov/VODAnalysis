"""
Google Sheets writer (gspread + service account).

Per match, in order:
  1. write_maps_entry()  → append a row to "Maps Entry"
  2. clear_per_round()   → wipe data rows in "PerRoundStat" (keep header + template)
  3. write_per_round()   → fill "PerRoundStat" with this match's rounds
  4. append_storage()    → append the rounds to "Storage"

Maps the LOCAL round dict (our_kills / our_deaths / traded / kost / ok_od / plant)
onto the sheet's per-player columns. Operators / Site / Strat are left blank for
manual entry. gspread is imported here only — the rest of the pipeline doesn't
depend on it.
"""

from typing import List, Dict, Any

import gspread
from google.oauth2.service_account import Credentials

import config

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def service_account_email() -> str:
    """Read the service account's client_email from the creds JSON."""
    import json
    with open(config.CREDENTIALS_PATH, encoding="utf-8") as f:
        return json.load(f).get("client_email", "(unknown)")


def get_client() -> gspread.Client:
    import os
    if not os.path.exists(config.CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"Service-account JSON not found at {config.CREDENTIALS_PATH}\n"
            "Put your key there (or set env GOOGLE_CREDENTIALS)."
        )
    creds = Credentials.from_service_account_file(config.CREDENTIALS_PATH, scopes=_SCOPES)
    return gspread.authorize(creds)


def open_sheet(sheet_id: str = None) -> gspread.Spreadsheet:
    return get_client().open_by_key(sheet_id or config.SHEET_ID)


def get_next_match_id(sheet: gspread.Spreadsheet) -> str:
    """Next ID like #030 from existing rows in Maps Entry."""
    ws = sheet.worksheet(config.SHEET_MAPS_ENTRY)
    ids = [v for v in ws.col_values(1) if v.startswith("#")]
    nums = [int(v[1:]) for v in ids if v[1:].isdigit()]
    return f"#{max(nums) + 1:03d}" if nums else "#001"


def get_player_order(sheet: gspread.Spreadsheet, tab: str) -> List[str]:
    """Player slot order from the 'X Operator' columns in row 1."""
    headers = sheet.worksheet(tab).row_values(1)
    players = []
    for h in headers:
        if h.endswith(" Operator"):
            name = h[: -len(" Operator")].strip()
            if name and name not in players:
                players.append(name)
    return players


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def _short(nick: str) -> str:
    """Strip team tag and map to the sheet short name via ROSTER_ALIASES."""
    if not nick:
        return ""
    base = nick.split(".")[0]
    aliases = config.ROSTER_ALIASES
    if base in aliases:
        return aliases[base]
    for k, v in aliases.items():
        if k.lower() == base.lower():
            return v
    return base


def _build_row(rd: Dict, match: Dict, headers: List[str],
               sheet_players: List[str], is_first: bool) -> List[Any]:
    row: List[Any] = [""] * len(headers)

    def put(col: str, val: Any):
        if col in headers:
            row[headers.index(col)] = "" if val is None else val

    if is_first:
        put("ID",   match.get("match_id", ""))
        put("Date", match.get("date", ""))
        put("VS.",  match.get("opponent", ""))
        put("Map",  match.get("map", ""))

    put("Round",  rd.get("round", ""))
    put("Side",   rd.get("side", ""))
    put("Result", rd.get("result", ""))
    put("Plant?", bool(rd.get("plant", False)))
    put("Who?",   _short(rd.get("plant_by")))
    put("OK/OD",  rd.get("ok_od", ""))
    put("Who? 2", rd.get("ok_od_who", ""))
    # Site, Strat, Time, 1vX → left blank (manual / not detected)

    our_kills  = rd.get("our_kills", {})
    our_deaths = rd.get("our_deaths", {})
    traded     = rd.get("traded", {})
    kost       = rd.get("kost", {})

    dt_cols = [h for h in headers if "Death Traded?" in h]
    wt_cols = [h for h in headers if "Who Traded?" in h]

    for i, sp in enumerate(sheet_players):
        put(f"{sp} Operator", "")                       # manual
        put(f"{sp} Kills",  our_kills.get(sp, 0))
        put(f"{sp} Death?", sp in our_deaths)
        if i < len(dt_cols):
            row[headers.index(dt_cols[i])] = sp in traded
        if i < len(wt_cols):
            row[headers.index(wt_cols[i])] = traded.get(sp, "")
        if sp in kost:
            put(f"KOST {sp}", kost[sp])

    return row


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def write_maps_entry(sheet: gspread.Spreadsheet, match: Dict, rounds: List[Dict]):
    wins   = sum(1 for r in rounds if r.get("result") == "WIN")
    losses = sum(1 for r in rounds if r.get("result") == "LOSS")
    result = "WIN" if wins > losses else "LOSS" if losses > wins else "DRAW"
    row = [
        match.get("match_id", ""), match.get("date", ""), match.get("opponent", ""),
        match.get("link", ""), match.get("map", ""),
        wins, losses, wins + losses, result, match.get("start_side", ""),
    ]
    sheet.worksheet(config.SHEET_MAPS_ENTRY).append_row(
        row, value_input_option="USER_ENTERED")


def _col_letter(n: int) -> str:
    if n <= 26:
        return chr(64 + n)
    return chr(64 + (n - 1) // 26) + chr(64 + (n - 1) % 26 + 1)


def clear_per_round(sheet: gspread.Spreadsheet):
    """Wipe data rows START..END in PerRoundStat (header + template untouched)."""
    ws = sheet.worksheet(config.SHEET_PER_ROUND)
    num_cols = max(len(ws.row_values(1)), 45)
    rng = (f"A{config.PERROUND_DATA_START_ROW}:"
           f"{_col_letter(num_cols)}{config.PERROUND_DATA_END_ROW}")
    ws.batch_clear([rng])


def write_per_round(sheet: gspread.Spreadsheet, rounds: List[Dict], match: Dict):
    ws = sheet.worksheet(config.SHEET_PER_ROUND)
    headers = ws.row_values(1)
    sheet_players = get_player_order(sheet, config.SHEET_PER_ROUND)
    rows = [_build_row(r, match, headers, sheet_players, is_first=(i == 0))
            for i, r in enumerate(rounds)]
    if rows:
        ws.update(f"A{config.PERROUND_DATA_START_ROW}", rows,
                  value_input_option="USER_ENTERED")


def append_storage(sheet: gspread.Spreadsheet, rounds: List[Dict], match: Dict):
    ws = sheet.worksheet(config.SHEET_STORAGE)
    headers = ws.row_values(1)
    sheet_players = get_player_order(sheet, config.SHEET_STORAGE)
    rows = [_build_row(r, match, headers, sheet_players, is_first=(i == 0))
            for i, r in enumerate(rounds)]
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def write_match(rounds: List[Dict], match: Dict, sheet_id: str = None,
                progress=print) -> Dict:
    """
    Full write: Maps Entry → PerRoundStat (clear+fill) → Storage.
    *progress(msg)* is called with status strings. Returns a summary dict.
    """
    sheet = open_sheet(sheet_id)

    progress("Writing Maps Entry…")
    write_maps_entry(sheet, match, rounds)

    progress("Clearing + writing PerRoundStat…")
    clear_per_round(sheet)
    write_per_round(sheet, rounds, match)

    progress("Appending to Storage…")
    append_storage(sheet, rounds, match)

    return {"sheet_id": sheet_id or config.SHEET_ID,
            "title": sheet.title, "rounds": len(rounds)}
