"""
Project-wide configuration — LOCAL OCR pipeline (Tesseract + OpenCV).

No external paid APIs. Results are printed to the console for manual entry
into Google Sheets.
"""

import os

# -----------------------------------------------------------------------------
# Project paths — computed from this file's location, so they work regardless of
# the current working directory (config.py lives in src/, assets live at root).
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR  = os.path.join(BASE_DIR, "out")

# -----------------------------------------------------------------------------
# Tesseract OCR
# -----------------------------------------------------------------------------
# Path to the Tesseract executable. On Windows the default UB-Mannheim install
# location is below. Override with env var TESSERACT_CMD if installed elsewhere.
# -----------------------------------------------------------------------------
TESSERACT_CMD = os.getenv(
    "TESSERACT_CMD",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
)

# -----------------------------------------------------------------------------
# Frame sampling
# -----------------------------------------------------------------------------
# IMPORTANT: plant notifications ("X is activating the defuser" / "Your team
# has activated the defuser") stay on screen only ~3 seconds. To reliably catch
# them, the frame interval must be <= 3s. 3 is the max safe value; 2 is safer.
FRAME_INTERVAL_SEC = 3     # one frame every N seconds (postfacto, not realtime)

# -----------------------------------------------------------------------------
# Canonical resolution
# Screenshots / extracted frames vary in size. Every frame is resized to this
# canonical resolution before analysis so region coordinates stay stable.
# Calibrate regions.yaml against this resolution.
# -----------------------------------------------------------------------------
CANONICAL_W = 1920
CANONICAL_H = 1080

# -----------------------------------------------------------------------------
# Roster
#   SHEET_PLAYERS = the spreadsheet columns (ROLES / slots), fixed order.
#   ROSTER_NICKS  = default in-game base nick that plays each role (core roster),
#                   parallel to SHEET_PLAYERS. Override per match for flex subs
#                   (e.g. when Jumbastik subs Tora, set the Tora slot to Jumbastik).
# A killfeed nick is attributed to its role/column via this mapping.
# -----------------------------------------------------------------------------
SHEET_PLAYERS = ["MrBom", "Hope", "Trackz", "Tother", "Tora"]
ROSTER_NICKS  = ["MrBom.AB", "Hopeless.AB", "Saiin.AB", "UnkxwnTT", "Tora.AB"]

# -----------------------------------------------------------------------------
# HUD profile (regions.yaml)
# -----------------------------------------------------------------------------
REGIONS_YAML    = os.path.join(DATA_DIR, "regions.yaml")
DEFAULT_PROFILE = None   # None → use default_profile from regions.yaml

# -----------------------------------------------------------------------------
# Team tags (from nickname suffixes in the killfeed, e.g. "MrBom.PLR")
# OUR_TEAM_TAG identifies your team. Kills are classified our-kill / our-death
# by comparing killer/victim tags.
# -----------------------------------------------------------------------------
OUR_TEAM_TAG = "AB"

# -----------------------------------------------------------------------------
# Extra nick → role aliases (optional). The per-match lineup already maps the
# 5 active nicks to roles; add here only stable alternate spellings the OCR may
# produce, or bench players' nicks. Tags (.AB) are stripped; matching is
# case-insensitive with a fuzzy fallback.
# -----------------------------------------------------------------------------
ROSTER_ALIASES = {
    "codeTora":    "Tora",
    "TotherSnail": "Tother",
}

# -----------------------------------------------------------------------------
# Killfeed icon templates
# Between the two nicks there is ALWAYS one of two icons:
#   crosshair  = a KILL (elimination)  -> read it
#   down-arrow = a DBNO (knock)        -> skip it
# Template matching classifies the row AND locates the killer|victim split.
# Create the two templates with make_template.py.
# -----------------------------------------------------------------------------
TEMPLATE_DIR         = os.path.join(BASE_DIR, "templates")
# A row is a KILL if ANY of these icons matches best: crosshair (прицел) or
# bodykill. One colour version of each is enough — matching is polarity-invariant
# (handles white-on-blue, white-on-red AND black-on-white from a single template).
# (Assist icon has no template → assist rows won't match → correctly excluded.)
TEMPLATE_KILL_ICONS  = ["icon_kill.png", "icon_bodykill.png", "icon_crosshair.png"]
# A row is a DBNO (knock) — skip — if this matches best.
TEMPLATE_DBNO_ICONS  = ["icon_dbno.png"]
TEMPLATE_MATCH_THRESHOLD = 0.50          # min cv2.matchTemplate score to accept

# True  = a row counts as a kill ONLY if the kill icon (crosshair/skull) matches
#         stronger than DBNO → excludes knocks (confirmed kills only).
# False = any 2-nick row that isn't a confident DBNO is a kill (catches more,
#         but a knock can leak in if the DBNO template is weak).
KILLFEED_REQUIRE_KILL_ICON = True

# Killfeed special-text markers (parsed from the row text, not nicks)
FIRST_BLOOD_KEYWORDS = ["first blood"]   # opening kill/death marker
# Words that are NOT nicknames — rows whose "victim" is one of these are markers
KILLFEED_NON_NICK_WORDS = ["first blood", "headshot", "blood"]

# -----------------------------------------------------------------------------
# Killfeed layout (tune after calibration)
# Row height is a FRACTION of the frame height (resolution-independent).
# One killfeed entry is ~4-5% of screen height.
# -----------------------------------------------------------------------------
KILLFEED_ROW_HEIGHT_FRAC = 0.045   # one entry height / frame height
KILLFEED_MAX_ROWS        = 6       # max simultaneous entries to scan

# -----------------------------------------------------------------------------
# Plant detection — keywords in the center notification banner
# -----------------------------------------------------------------------------
PLANT_START_KEYWORDS   = ["activating the defuser", "activating"]      # start (has planter name)
PLANT_CONFIRM_KEYWORDS = ["has activated the defuser", "activated the defuser"]
PLANT_OURS_KEYWORDS    = ["your team"]   # distinguishes our plant from enemy

# -----------------------------------------------------------------------------
# Objective banner (orange text under the timer). Drives PHASE, SIDE and
# PLANT-STATE — all from one region we already OCR. Observed phrases:
#   DEF prep:    "fortify the bombsite"
#   DEF action:  "defend the bombsite"  |  "disable the defuser" (enemy planted)
#   ATK prep:    "search for bombsites" |  "position drones"
#   ATK action:  "pick up defuser" | "plant the defuser" | "escort the defuser"
#                | "defend the defuser" (WE planted — defuser is down)
# Matching is lowercase substring, so singular/plural variants are covered.
# -----------------------------------------------------------------------------
OBJ_PREP_KEYWORDS   = ["preparation", "fortify", "search for bomb", "position drone"]
OBJ_ACTION_KEYWORDS = ["defend", "disable", "escort", "plant the defuser",
                       "pick up", "defuser", "bombsite"]

# Side from objective phrasing (works during prep AND action)
OBJ_SIDE_DEF = ["fortify", "defend the bombsite", "disable", "defend the bomb"]
OBJ_SIDE_ATK = ["search for bomb", "position drone", "escort", "plant the defuser",
                "pick up", "defend the defuser"]

# Defuser is PLANTED (down) when the objective says:
#   "defend the defuser" → WE (ATK) planted   |   "disable the defuser" → enemy planted
OBJ_PLANTED_OURS  = ["defend the defuser"]     # only valid while our side is ATK
OBJ_PLANTED_ENEMY = ["disable the defuser"]    # only valid while our side is DEF

# -----------------------------------------------------------------------------
# Killfeed deduplication
# A kill entry lingers on screen for several seconds, so the same event shows
# up across multiple frames. Suppress repeats of the same (killer, victim)
# pair seen within this time window.
# -----------------------------------------------------------------------------
DEDUP_WINDOW_SEC = 8

# -----------------------------------------------------------------------------
# Trade detection
# A teammate's death is "traded" if another teammate kills the SAME enemy
# (the one who got the kill) within this many seconds. If a teammate kills a
# DIFFERENT enemy in the window, it's a gray area → flagged with timing.
# Note: frame sampling (~3s) limits timing precision; borderline cases are flagged.
# -----------------------------------------------------------------------------
TRADE_WINDOW_SEC = 5

# -----------------------------------------------------------------------------
# OCR preprocessing (tune these for your footage)
# -----------------------------------------------------------------------------
OCR_UPSCALE = 4            # upscale factor before OCR (bigger = better for small text)
OCR_INVERT  = True         # most HUD text is light-on-dark → invert to dark-on-light

# White-text mask threshold (channel="white"): a pixel is "white text" if
# min(B,G,R) >= this. Lower if white text is dim; raise if the colored bar bleeds in.
WHITE_TEXT_THRESH = 150

# Adaptive (local) threshold for same-hue text (blue-on-blue / red-on-red score).
# BLOCK = neighbourhood size (odd); C = bias subtracted from local mean.
ADAPTIVE_BLOCK = 15
ADAPTIVE_C     = 8

# -----------------------------------------------------------------------------
# Known R6 Mobile operators — OCR output is fuzzy-matched against this list.
# Edit to match the operators that actually appear in your matches.
# -----------------------------------------------------------------------------
OPERATORS_ATK = [
    "Sledge", "Thatcher", "Ash", "Thermite", "Twitch", "Glaz", "Buck",
    "Blackbeard", "Capitao", "Hibana", "Jackal", "Ying", "Zofia", "Dokkaebi",
    "Lion", "Finka", "Maverick", "Nomad", "Gridlock", "Nokk", "Amaru",
    "Kali", "Iana", "Ace", "Zero", "Flores", "Osa", "Sens", "Grim", "Brava",
]
OPERATORS_DEF = [
    "Smoke", "Mute", "Castle", "Pulse", "Doc", "Rook", "Kapkan", "Tachanka",
    "Jager", "Bandit", "Frost", "Valkyrie", "Caveira", "Echo", "Mira",
    "Lesion", "Ela", "Vigil", "Maestro", "Alibi", "Clash", "Kaid", "Mozzie",
    "Warden", "Goyo", "Wamai", "Oryx", "Melusi", "Aruni", "Thunderbird",
    "Thorn", "Azami", "Solis", "Fenrir",
]
OPERATORS_ALL = OPERATORS_ATK + OPERATORS_DEF

# Minimum fuzzy-match score (0-100) to accept an OCR→operator/name correction
FUZZY_MIN_SCORE = 70
