# R6M VOD Analyzer

Локальный анализ записей матчей **Rainbow Six Mobile**: читает killfeed, таймер,
результат раунда, плант, OK/OD, трейды — и печатает готовую таблицу в консоль
для ручного переноса в Google Sheets.

**Без платных API.** Всё локально: Tesseract (OCR) + OpenCV (template matching).

---

## 1. Установка

```powershell
pip install -r requirements.txt
winget install UB-Mannheim.TesseractOCR     # сам OCR-движок
winget install Gyan.FFmpeg                   # для извлечения кадров из видео
```

Проверка:
```powershell
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
ffmpeg -version
```
Если Tesseract стоит в другом месте — пропиши путь:
```powershell
$env:TESSERACT_CMD="C:\твой\путь\tesseract.exe"
```

---

## 2. Приложение (GUI) — самый простой способ

Двойной клик по **`run_app.bat`** (или `python src\app.py`) откроет окно с 3 вкладками:

- **Analyze** — выбираешь источник (видео/папка/YouTube), профиль, сторону,
  игроков, таймкоды → кнопка **Run** → прогресс-бар → таблица раундов +
  предупреждения. Отчёт сохраняется автоматически.
- **Calibrate** — грузишь кадр, выбираешь регион, **двигаешь слайдеры x/y/w/h**
  с живым превью рамок, жмёшь **Read region (OCR)** чтобы проверить распознавание,
  **Save** пишет доли обратно в `regions.yaml`. Никакого ручного редактирования YAML.
- **Test OCR** — грузишь кадр → **Run** → аннотированная картинка со всеми
  регионами + полный разбор: фаза, плант, счёт, таймер, killfeed, OCR по каждому региону.

Всё остальное ниже — то же самое через командную строку (если удобнее скриптом).

---

## 2b. Командная строка

Самый частый случай — **файл с телефона** (присланный в Telegram **как файл**, не как видео):

```powershell
python src\analyze_local.py --video "C:\path\record.mp4"
```

Скрипт спросит в терминале:
- **Профиль HUD** (если их несколько; сейчас один — возьмёт автоматически)
- **Стартовую сторону** раунда 1 (ATK/DEF)
- **4-го и 5-го игрока** (короткие имена для таблицы)
- **Таймкоды** — запись может начинаться с лобби, поэтому предложит обрезать:
  ```
  The recording may include lobby/prep before the match.
  Trim to a time range? [y/n]
    Start (HH:MM:SS, blank = beginning):
    End   (HH:MM:SS, blank = end):
  ```

Всё можно задать и флагами, чтобы не спрашивал:
```powershell
python src\analyze_local.py --video "record.mp4" `
  --start 00:05:30 --end 00:38:00 `
  --start-side ATK --slot3 JLA --slot4 Trackz --profile tora_default
```

### Другие источники

```powershell
# Папка готовых скриншотов (по порядку имён файлов)
python src\analyze_local.py --frames .\hud_samples

# YouTube VOD (скачает сегмент)
python src\analyze_local.py --url "https://youtu.be/XXXX" --start 01:12:30 --end 01:38:45
```

---

## 3. Выбор профиля HUD

Профили — это раскладки HUD разных игроков (координаты регионов). Хранятся в
`data/regions.yaml` под `profiles:`.

- **Через терминал**: запусти без `--profile` — если профилей несколько, покажет
  список и спросит номер. Если профиль один — возьмёт его автоматически.
- **Флагом**: `--profile tora_default`
- **Список профилей**: `python src\calibrate.py --list-profiles`

Добавить новый профиль: скопируй блок профиля в `regions.yaml`, переименуй ключ,
поправь координаты (см. §5).

---

## 4. Что выводится

Во время анализа — **прогресс-бар** (кадров обработано / осталось времени).
Анализ **параллельный** (`--workers N`, по умолчанию = ядра CPU, до 8) — на
i5-12450H это в ~5-6 раз быстрее последовательного.

После анализа отчёт **автоматически сохраняется**:
```
out/<profile>/reports/report_<дата>.txt    — копия консольного вывода
out/<profile>/reports/rounds_<дата>.json   — сырые данные раундов
```
Так что вывод не потеряется, даже если закрыть терминал.

Две таблицы:

- **Frame Timeline** — каждый проанализированный кадр (фаза, таймер, киллы). Подстраховка.
- **Round Summary** — агрегат по раундам, готов к переносу:

| # | Side | Score | Res | OK/OD | Plant | Our Kills | Our Deaths (← killer / trade) |
|---|------|-------|-----|-------|-------|-----------|-------------------------------|
| 5 | ATK | 2-2 | WIN | OK MrBom | ✓ MrBom | MrBom ×2, Tora ×1 | Hope ← drayn.s7 [traded by Tora] |

Серые зоны трейдов и «сбитые планты» выводятся отдельным списком Warnings.

---

## 5. Калибровка под свой HUD (один раз на профиль)

Нужна, когда добавляешь профиль или HUD сместился. Подробно — в
[docs/SETUP_LOCAL.md](docs/SETUP_LOCAL.md). Кратко:
**Проще всего — вкладка Calibrate в GUI** (слайдеры + живое превью). Ниже — CLI-вариант.

1. **Кадр для калибровки** (нативное разрешение телефона, напр. 2688×1216):
   ```powershell
   ffmpeg -ss 00:10:00 -i "record.mp4" -frames:v 1 -q:v 2 hud_samples/native.png
   ```
2. **Регионы**: `python src\calibrate.py hud_samples/native.png` → правь доли в
   `data/regions.yaml` (координаты в долях 0..1, не в пикселях) → повторяй.
3. **Шаблоны иконок килфида** (3 штуки) из того же кадра:
   ```powershell
   python src\make_template.py hud_samples/native.png --name icon_kill    # прицел
   python src\make_template.py hud_samples/native.png --name icon_kill2   # череп
   python src\make_template.py hud_samples/native.png --name icon_dbno    # стрелка вниз (knock)
   ```
4. **Маппинг ников**: в `src/config.py` → `ROSTER_ALIASES` впиши игровые ники → короткие
   имена (напр. `"codeTora": "Tora"`).
5. **Проверка**: `python src\test_ocr.py hud_samples/native.png` — покажет что распознал.

---

## 6. Конфиг (`src/config.py`) — основное

| Параметр | Что |
|---|---|
| `TESSERACT_CMD` | путь к tesseract.exe |
| `FRAME_INTERVAL_SEC` | интервал кадров (3 — чтобы ловить 3-сек уведомления планта) |
| `OUR_TEAM_TAG` | суффикс твоей команды в никах (напр. `AB`, `PLR`) |
| `ROSTER_ALIASES` | игровой ник → короткое имя для таблицы |
| `TRADE_WINDOW_SEC` | окно трейда (5 сек) |
| `TEMPLATE_KILL_ICONS` / `TEMPLATE_DBNO_ICONS` | имена файлов шаблонов |
| `OPERATORS_ATK/DEF` | список операторов (для будущего; сейчас операторы вручную) |

---

## 7. Что автоматически, что вручную

**Авто:** killfeed (киллы/смерти по `.TAG`), таймер, фаза, результат раунда,
счёт, номер раунда, плант (start+who+confirm), OK/OD, трейды.

**Вручную** (вписываешь в Sheets сам): операторы (в HUD они иконки), сайт, страт.

---

## Структура проекта

```
run_app.bat          ← запуск GUI (двойной клик)
README.md
requirements.txt

src/                 ← весь код
  app.py             GUI-приложение (3 вкладки)
  analyze_local.py   движок анализа (используется и GUI, и CLI)
  calibrate.py       калибровка регионов (CLI)
  make_template.py   нарезка шаблонов иконок килфида
  test_ocr.py        тест распознавания на одном кадре
  config.py          настройки
  phase_detector.py  фаза раунда + счёт/номер раунда
  killfeed_reader.py killfeed → киллы (template + OCR + .TAG)
  plant_reader.py    детект планта
  timer_reader.py    таймер
  dedup.py           дедуп килфида
  console_report.py  вывод таблиц
  regions_loader.py  загрузка регионов + кроп (дробные координаты)
  ocr_utils.py       препроцессинг + Tesseract + fuzzy
  video_processor.py скачивание/нарезка видео (yt-dlp + ffmpeg)

data/regions.yaml    координаты регионов (профили, доли 0..1)
templates/           icon_kill.png, icon_kill2.png, icon_dbno.png
hud_samples/         твои скриншоты для калибровки
docs/SETUP_LOCAL.md  детальный гайд по калибровке
out/                 сгенерированные отчёты (автосейв)
```
