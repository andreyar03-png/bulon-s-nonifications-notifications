"""Чтение контент-планов (Google Drive/Sheets) и слотов операторов (Google Calendar).

Только чтение. Разбор эвристиками (колонки по ключевым словам + словарь синонимов),
LLM — только fallback для строк, которые правила не разобрали. Пересканирование раз
в сутки, результат в SQLite.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pickle
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import db
from rules import POST, REEL, STORY, Item

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
# порядок важен: "сторис" проверяется до "пост", "пост-закреп" ловится по "пост"
TYPE_WORDS = [
    ("рилс", REEL), ("рилз", REEL), ("reels", REEL), ("reel", REEL), ("видео", REEL),
    ("сторис", STORY), ("stories", STORY), ("story", STORY), ("сториз", STORY),
    ("пост", POST), ("post", POST), ("карусел", POST),
]


def _svc(name, ver):
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS", "credentials.json"), scopes=SCOPES
    )
    return build(name, ver, credentials=creds, cache_discovery=False)


# ---------- разбор ----------

def parse_date(s, today=None, year_hint=None):
    """Дата из ячейки: ISO, 12.05, 12.05.2026, «12 мая». None — если не дата.

    year_hint — год, если он есть только в названии листа («Февраль-2026», «4.2» без
    года внутри строки): без этого угадывание «ближайшего к сегодня года» может уйти
    не в ту сторону, когда сегодня конец года, а лист — про начало следующего/прошлого.
    """
    today = today or dt.date.today()
    s = str(s).strip().lower()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", s)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else None
            if y and y < 100:
                y += 2000
        else:
            m = re.search(r"\b(\d{1,2})\s*([а-я]{3,})", s)
            if not m or m.group(2)[:3] not in MONTHS:
                return None
            d, mo, y = int(m.group(1)), MONTHS[m.group(2)[:3]], None
    if y:
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None
    if year_hint:
        try:
            return dt.date(year_hint, mo, d)
        except ValueError:
            return None
    # Года нет и в листе не указан («12 июня» без года в названии): берём ближайший к сегодня.
    best = None
    for yy in (today.year - 1, today.year, today.year + 1):
        try:
            c = dt.date(yy, mo, d)
        except ValueError:
            continue
        if best is None or abs((c - today).days) < abs((best - today).days):
            best = c
    return best


def norm_type(s):
    s = str(s).lower()
    for word, ctype in TYPE_WORDS:
        if word in s:
            return ctype
    return None


FIELD_KEYS = {   # regex, порядок = приоритет; \b нужен, иначе «вид» ловится в «картинку/видео»
    "date": (r"дата", r"\bdate\b"),
    "type": (r"\bвид\b", r"формат", r"\bтип"),
    "topic": (r"тема", r"\btopic", r"описание", r"иде[яи]", r"контент", r"текст"),
}


def find_cols(header):
    """Колонки по ключевым словам, с приоритетом внутри поля: в одном листе бывают
    и «Вид», и «Формат контента» (тип — «Вид»), и «Тема» рядом с «Save Rate (…контент)».
    """
    hs = [str(h).strip().lower() for h in header]
    idx, used = {}, set()
    for field, keys in FIELD_KEYS.items():
        best = None
        for i, h in enumerate(hs):
            if not h or i in used:
                continue
            for prio, k in enumerate(keys):
                if re.search(k, h):
                    if best is None or prio < best[0]:
                        best = (prio, i)
                    break
        if best:
            idx[field] = best[1]
            used.add(best[1])
    return idx


def _sheet_month(name):
    """Месяц по названию листа («Февраль-2026», «июнь-июль» -> июнь), иначе None."""
    return next((m for k, m in MONTHS.items() if k in name.lower()), None)


def sheet_in_scope(name, today=None):
    """Актуален ли лист: текущий/следующий месяц (или месяц не угадывается — тогда
    лист не месячный, не фильтруем). Форматы КП меняются от месяца к месяцу, старые
    листы — архив на своих собственных условиях, не смешиваем их с текущей работой."""
    today = today or dt.date.today()
    m = _sheet_month(name)
    return m is None or m in (today.month, today.month % 12 + 1)


def parse_sheet(project, sheet, rows, today=None, merges=()):
    """-> (items, undated, unparsed). Строки без даты и темы (аналитика, легенды) отбрасываются.

    merges — объединённые диапазоны листа [(row0, row1, col0, col1)]: в объединённой
    ячейке даты API отдаёт значение только первой строке, остальные наследуют.
    """
    header_at = None
    cols = {}
    for i, row in enumerate(rows[:15]):
        c = find_cols(row)
        if "date" in c and ("type" in c or "topic" in c):
            header_at, cols = i, c
            break
    if header_at is None:
        return [], [], []
    today = today or dt.date.today()

    def merged_date(r):
        c = cols["date"]
        return any(r0 <= r < r1 and c0 <= c < c1 for r0, r1, c0, c1 in merges)

    # в части планов лист = месяц, а в колонке даты просто число («| пятница | 3 | ...»)
    sheet_month = _sheet_month(sheet)
    # год из названия листа («Февраль-2026», «Июль-26») — надёжнее, чем угадывать
    ym = re.search(r"(20\d{2})|-(\d{2})\b", sheet)
    year_hint = int(ym.group(1) or f"20{ym.group(2)}") if ym else None

    items, undated, unparsed, last_date = [], [], [], None
    for offset, row in enumerate(rows[header_at + 1:]):
        def cell(key):
            i = cols.get(key)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        date_s, type_s, topic = cell("date"), cell("type"), cell("topic")
        d = parse_date(date_s, today, year_hint)
        if d is None and sheet_month and date_s.isdigit():
            d = parse_date(f"{date_s}.{sheet_month}", today, year_hint)
        if d:
            last_date = d
        elif not date_s and merged_date(header_at + 1 + offset):
            d = last_date  # объединённая дата: несколько публикаций в один день
        if not topic and not type_s:
            continue  # пустой слот календаря / аналитика / легенда — даже если дата есть
        if "рассылка" in type_s.lower():
            continue  # рассылки агентство здесь не ведёт
        # формат берём из колонки, иначе из темы («Рилс в формате: диалог», «Пост-карусель…»)
        ctype = norm_type(type_s) or norm_type(topic)
        raw = " | ".join(x for x in (date_s, type_s, topic) if x)
        if ctype is None and "type" not in cols:
            ctype = POST  # в листе нет колонки формата — по умолчанию текстовый контент
        if ctype is None:
            ctype = llm_type(raw)
        if ctype is None:
            if d is None or d >= today:  # прошлое не актуально предупреждать — уже выложено
                unparsed.append(f"{project} / {sheet}: {raw}")
            continue
        item = Item(project, sheet, ctype, d, topic or type_s)
        (items if d else undated).append(item)
    return items, undated, unparsed


def llm_type(raw):
    """Fallback-классификация одной строки через LLM. Ошибка API = None, бот не падает."""
    cached = db.llm_get(raw)
    if cached is not None:
        return cached or None
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"),
        "max_tokens": 8,
        "messages": [{"role": "user", "content":
            "Строка из контент-плана SMM-агентства. Определи тип контента. "
            "Ответь ровно одним словом: REEL, POST, STORY или NONE.\n\n" + raw}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            answer = json.load(r)["content"][0]["text"].strip().upper()
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError):
        return None
    ctype = answer if answer in (REEL, POST, STORY) else ""
    db.llm_set(raw, ctype)
    return ctype or None


# ---------- источники ----------

PLAN_NAME_RE = re.compile(r"контент[\s\-]?план|\bкп\b", re.I)


def _files(drive):
    """Все таблицы, доступные service account, с «КП»/«контент-план» в названии.

    Не обход конкретной папки: Drive отдаёт service account'у любой файл, до которого
    у него есть доступ — расшарен ли он напрямую или лежит внутри расшаренной папки,
    на любой глубине. Так что расшаривать нужно только папки проектов (сколько угодно,
    в корне или где угодно ещё), а не сообщать боту их id.
    В папке проекта кроме контент-плана бывают медиапланы/брифы/отчёты — отсекаем их
    по названию.
    """
    files, token = [], None
    while True:
        res = drive.files().list(
            q="mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            fields="nextPageToken,files(id,name,parents)", pageSize=1000, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files += [f for f in res["files"] if PLAN_NAME_RE.search(f["name"])]
        token = res.get("nextPageToken")
        if not token:
            break

    # проект = название папки, где лежит КП (полное, не аббревиатура из имени файла)
    folders = {}
    for pid in {f["parents"][0] for f in files if f.get("parents")}:
        try:
            folders[pid] = drive.files().get(
                fileId=pid, fields="name", supportsAllDrives=True).execute()["name"]
        except Exception:  # доступа к папке нет (странное расшаривание) — берём имя файла
            pass
    for f in files:
        f["project"] = folders.get((f.get("parents") or [None])[0], f["name"])
    return files


def scan(today=None):
    """Обход всех таблиц -> (items, undated, unparsed)."""
    drive, sheets = _svc("drive", "v3"), _svc("sheets", "v4")
    files = _files(drive)

    items, undated, unparsed = [], [], []
    for f in files:
        meta = sheets.spreadsheets().get(
            spreadsheetId=f["id"], fields="sheets(properties.title,merges)").execute()
        sh = [s for s in meta["sheets"] if sheet_in_scope(s["properties"]["title"], today)]
        titles = [s["properties"]["title"] for s in sh]
        merges = [[(m.get("startRowIndex", 0), m.get("endRowIndex", 0),
                    m.get("startColumnIndex", 0), m.get("endColumnIndex", 0))
                   for m in s.get("merges", [])] for s in sh]
        if not titles:
            continue  # все листы файла — старые месяцы, не актуален
        data = sheets.spreadsheets().values().batchGet(
            spreadsheetId=f["id"], ranges=[f"'{t}'!A1:Z500" for t in titles],
        ).execute().get("valueRanges", [])
        for title, mg, vr in zip(titles, merges, data):
            a, b, c = parse_sheet(f["project"], title, vr.get("values", []), today, mg)
            items += a
            undated += b
            unparsed += c
    return items, undated, unparsed


def load(force=False, today=None):
    """Кэш на сутки. При ошибке скана отдаём прошлый кэш (бот не должен молчать)."""
    today = today or dt.date.today()
    row = db.cache_get()
    if row and row[0] == today.isoformat() and not force:
        return pickle.loads(row[1])
    try:
        data = scan(today)
    except Exception as e:  # noqa: BLE001 — сеть/квоты/доступ: работаем на старом кэше
        if row:
            items, undated, unparsed = pickle.loads(row[1])
            return items, undated, unparsed + [f"контент-планы не пересканированы: {e}"]
        raise
    db.cache_set(today.isoformat(), pickle.dumps(data))
    return data


# В агентском календаре слот доступности — «<Имя> может»/«<Имя> свободен/свободна»,
# бронь — «Съемка <проект>: <Имя>» (встречается и «<Проект> съемка: <Имя>»).
# Всё остальное — планёрки, созвоны, аудиты — не слоты.
SLOT_RE = re.compile(r"^([^\W\d_]+)\s+(?:может|своб)", re.I)
SHOOT_RE = re.compile(r"съ[её]мка", re.I)


def _event_days(e):
    """Все даты события: слот «Александр может» часто ставят на несколько дней сразу
    (Google рисует его одной сплошной плашкой) — end.date у all-day событий эксклюзивный."""
    start = e["start"].get("dateTime") or e["start"].get("date")
    if not start:
        return []
    d0 = dt.date.fromisoformat(start[:10])
    if e["start"].get("date"):  # all-day: диапазон, а не одна дата
        end = e["end"].get("date")
        d1 = dt.date.fromisoformat(end[:10]) if end else d0 + dt.timedelta(days=1)
        return [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days)]
    return [d0]


def classify(events):
    """Сырые события календаря -> {date: [свободные операторы]}.

    День, на который оператор уже забронирован под съёмку, из его слотов вычитается.
    """
    free, busy = {}, {}
    for e in events:
        s = (e.get("summary") or "").strip()
        m = SLOT_RE.match(s)
        is_shoot = SHOOT_RE.search(s) and ":" in s
        if not m and not is_shoot:
            continue
        for d in _event_days(e):
            if m:
                free.setdefault(d, set()).add(m.group(1))
            else:
                busy.setdefault(d, set()).add(s.rsplit(":", 1)[-1].strip())
    out = {}
    for d, ops in free.items():
        if ops - busy.get(d, set()):
            out[d] = sorted(ops - busy.get(d, set()))
    return out


def slots(start: dt.date, end: dt.date):
    """Свободные слоты операторов из агентского календаря -> {date: [имена]}."""
    msk = ZoneInfo("Europe/Moscow")
    ev = _svc("calendar", "v3").events().list(
        calendarId=os.environ["CALENDAR_ID"],
        timeMin=dt.datetime.combine(start, dt.time(), msk).isoformat(),
        timeMax=dt.datetime.combine(end, dt.time(23, 59), msk).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=2500,
    ).execute().get("items", [])
    return classify(ev)
