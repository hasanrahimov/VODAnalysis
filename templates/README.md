# Killfeed icon templates

Put two grayscale PNGs here (create them with `make_template.py`):

- `icon_kill.png` — the crosshair / skull icon shown between killer and victim
  for a real KILL.
- `icon_dbno.png` — the down-arrow icon shown for a DBNO (knock, not a kill).

The killfeed reader template-matches each killfeed row against these two icons.
KILL rows go to OCR; DBNO rows are skipped (cheap filter before expensive OCR).

How to create:
```
python make_template.py hud_samples/02_action.png --name icon_kill
python make_template.py hud_samples/03_dbno.png   --name icon_dbno
```

Crop tightly around just the icon — no surrounding text.
```
