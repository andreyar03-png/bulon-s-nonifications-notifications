"""Проверка расчёта сроков и разбора контент-плана: python test_rules.py"""
import datetime as dt
import os

os.environ.setdefault("DB_PATH", "data/test.db")
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "1")

import bot
import plans
import rules
from rules import POST, REEL, STORY, Item

D = dt.date


def test_workdays():
    assert rules.wd(D(2026, 5, 11), -1) == D(2026, 5, 8)      # пн -1 -> пт
    assert rules.wd(D(2026, 5, 16), -1) == D(2026, 5, 15)     # сб -1 -> пт
    assert rules.wd(D(2026, 5, 17), -3) == D(2026, 5, 13)     # вс -3 -> ср
    assert rules.wd(D(2026, 5, 8), 1) == D(2026, 5, 11)       # пт +1 -> пн
    assert rules.is_workday(D(2026, 5, 9)) is False


def test_reel():
    it = Item("LE", "май", REEL, D(2026, 5, 15), "тема")      # выкладка пт
    t = {x.kind: x.date for x in rules.tasks(it)}
    assert t["shoot"] == D(2026, 5, 8)          # -5 р.д.
    assert t["script"] == D(2026, 5, 1)         # -5 р.д. от съёмки
    assert t["script_ok"] == D(2026, 5, 5)      # -3 р.д. от съёмки
    assert t["storyboard"] == D(2026, 5, 6)
    assert t["edit"] == D(2026, 5, 11)          # след. раб. день после съёмки
    assert rules.edit_end(it) == D(2026, 5, 12) # монтаж 2 р.д.
    assert t["client"] == D(2026, 5, 14)        # -1 р.д. до выкладки


def test_post_weekend_publish():
    it = Item("LE", "май", POST, D(2026, 5, 17), "тема")      # выкладка вс
    t = {x.kind: x.date for x in rules.tasks(it)}
    assert (t["text"], t["image"], t["client"]) == (D(2026, 5, 13), D(2026, 5, 14), D(2026, 5, 15))


def test_shoot_spread():
    """Съёмки разных проектов в одно окно при слотах на разные дни -> разные даты."""
    a = Item("A", "s", REEL, D(2026, 5, 15), "a")
    b = Item("B", "s", REEL, D(2026, 5, 15), "b")
    slots = {D(2026, 5, 7): ["оп1"], D(2026, 5, 8): ["оп2"]}
    (_, ca), (_, cb) = rules.suggest_shoots([a, b], slots)
    assert ca[0] != cb[0]
    assert rules.suggest_shoots([a], {})[0][1] == []          # нет слотов -> нет кандидатов


def test_shoot_batching():
    """До 5 рилсов одного проекта — одна съёмка (одна пачка), не 5 отдельных слотов."""
    five = [Item("A", "s", REEL, D(2026, 5, 15), f"тема{i}") for i in range(5)]
    sixth = Item("A", "s", REEL, D(2026, 5, 16), "тема6")
    # окно первой пачки (анкер 8 мая): 7/8/11 мая; окно второй (анкер 11 мая): 8/11/12 —
    # слоты только на не пересекающихся датах, чтобы пачки не могли выбрать одно и то же
    slots = {D(2026, 5, 7): ["оп1"], D(2026, 5, 12): ["оп2"]}
    out = rules.suggest_shoots(five + [sixth], slots)
    assert len(out) == 2                        # 6 рилсов -> 2 пачки (5 + 1)
    assert len(out[0][0]) == 5 and len(out[1][0]) == 1
    assert out[0][1] == [D(2026, 5, 7)]
    assert out[1][1] == [D(2026, 5, 12)]


def test_parse_real():
    """Реальная шапка и строки из «Контент-план_Флобериум»."""
    rows = [
        ["", "", "", "", "", "", "", ""],
        ["Дата", "День недели", "Соцсети", "Формат", "Тема",
         "Ссылка на текст/описание", "Ссылка на картинку/видео", "Статус"],
        ["12 июня", "Пт", "вконтакте, инст", "пост", "", "", "", "Пишем текст"],
        ["13 июня", "сб", "", "", "", "", "", "Пишем текст"],          # пустой слот -> мимо
        ["14 июня", "вс", "вконтакте, инст", "рилс", "", "", "", "Пишем текст"],
        ["17 июня", "ср", "", "", "Рилс с анонсом конкурса", "", "", ""],   # формат из темы
        ["20 июня", "сб", "", "", "Пост-карусель про мастерскую", "", "", ""],
        ["", "", "", "сторис", "опрос", "", "", ""],                   # без даты -> варнинг
    ]
    items, undated, unparsed = plans.parse_sheet("Флобериум", "июнь", rows, today=D(2026, 6, 1))
    assert [(i.ctype, i.pub_date) for i in items] == [
        (POST, D(2026, 6, 12)), (REEL, D(2026, 6, 14)),
        (REEL, D(2026, 6, 17)), (POST, D(2026, 6, 20))]
    assert len(undated) == 1 and undated[0].ctype == "STORY"
    assert unparsed == []   # LLM не дёргается: нераспознанных строк нет


def test_parse_sheet_without_format_column():
    """Лист «Дата | День недели | Контент | Статус» — колонки формата нет вообще."""
    rows = [
        ["Дата", "День недели", "Контент", "Статус"],
        ["22.09.2025", "пн", "Вопрос", ""],
        ["1.11.2025", "сб", "Объявляем конкурс рассказов", ""],
    ]
    items, _, unparsed = plans.parse_sheet("Флобериум", "осень", rows)
    assert [(i.ctype, i.pub_date) for i in items] == [
        (POST, D(2025, 9, 22)), (POST, D(2025, 11, 1))]
    assert unparsed == []


def test_parse_le():
    """Реальные шапка и строки из «Lets go english»: тип в «Вид», а не в «Формат контента»."""
    rows = [
        ["Дата", "День недели", "Вид", "Формат контента", "Тема", "Ссылка"],
        ["10.08", "Понедельник", "Пост-закреп", "Анонс", "Расписание пробных занятий", ""],
        ["14.08", "Пятница", "Reels", "Презентация", "Как говорит ребенок после обучения", ""],
        ["17.08", "Понедельник", "Пост-закреп", "", "Новый учебный год", ""],
    ]
    items, _, unparsed = plans.parse_sheet("LE", "август", rows, today=D(2026, 8, 1))
    assert [(i.ctype, i.pub_date) for i in items] == [
        (POST, D(2026, 8, 10)), (REEL, D(2026, 8, 14)), (POST, D(2026, 8, 17))]
    assert items[0].topic == "Расписание пробных занятий"   # не «Анонс»
    assert unparsed == []


def test_merged_date_cells():
    """Сторис и рилс на одну дату: объединённая ячейка -> дата только в первой строке."""
    rows = [
        ["Дата", "День недели", "Соцсети", "Формат", "Тема"],
        ["22 мая", "пятница", "вконтакте, инста", "сторис", "Напоминаем про тур"],
        ["", "", "вконтакте, инста", "рилс", "Анонс летних языковых туров"],
        ["", "", "вконтакте, инста", "сторис", "Тема без даты"],     # вне merge -> варнинг
    ]
    merges = [(1, 3, 0, 1)]     # объединены строки 1-2 колонки «Дата»
    items, undated, _ = plans.parse_sheet("LE", "май", rows, D(2026, 5, 1), merges)
    assert [(i.ctype, i.pub_date) for i in items] == [
        (STORY, D(2026, 5, 22)), (REEL, D(2026, 5, 22))]
    assert len(undated) == 1 and undated[0].topic == "Тема без даты"


def test_ignore_rassylka():
    rows = [
        ["Дата", "Формат", "Тема"],
        ["3 января", "рассылка", "Зовем на курс"],
        ["4 января", "пост", "Как проводите праздники?"],
    ]
    items, _, _ = plans.parse_sheet("Ф", "январь", rows, today=D(2026, 1, 1))
    assert [i.ctype for i in items] == [POST]




def test_sheet_in_scope():
    """Старые форматы КП в прошлых месяцах больше не актуальны — не читаем их вовсе."""
    today = D(2026, 8, 14)
    assert plans.sheet_in_scope("Август-2026", today) is True
    assert plans.sheet_in_scope("Сентябрь", today) is True     # следующий месяц — вперёд смотрим
    assert plans.sheet_in_scope("Январь-2026", today) is False
    assert plans.sheet_in_scope("июнь-июль", today) is False
    assert plans.sheet_in_scope("Лист1", today) is True         # без месяца в имени — не фильтруем
    assert plans.sheet_in_scope("Декабрь", D(2026, 12, 20)) is True
    assert plans.sheet_in_scope("Январь", D(2026, 12, 20)) is True  # декабрь -> январь, через год


def test_year_from_sheet_name():
    """«4.2» в листе «Февраль-2026» — год из даты не угадываем, берём из названия листа.
    Без этого, если сегодня конец года, ближайший-год эвристика уводит в 2027."""
    rows = [["Дата", "Формат", "Тема"], ["4.2", "пост", "Про худ.галерею"]]
    items, _, _ = plans.parse_sheet("Б", "Февраль-2026", rows, today=D(2026, 12, 20))
    assert items[0].pub_date == D(2026, 2, 4)


def test_unparsed_past_rows_suppressed():
    """Нераспознанная строка с прошедшей датой не должна маячить в предупреждениях вечно —
    её уже выложили, чинить нечего. С будущей/сегодняшней датой — предупреждать, как раньше."""
    rows = [
        ["Дата", "Формат", "Тема"],
        ["10 августа", "", "Загадочная тема без формата (прошлое)"],
        ["16 августа", "", "Загадочная тема без формата (сегодня)"],
        ["20 августа", "", "Загадочная тема без формата (будущее)"],
    ]
    _, _, unparsed = plans.parse_sheet("Ф", "август", rows, today=D(2026, 8, 16))
    assert len(unparsed) == 2
    assert all("прошлое" not in u for u in unparsed)


def test_parse_month_from_sheet_name():
    """Лист-месяц: в колонке даты только число, шапка английская."""
    rows = [
        ["", "Date", "", "Topic Для длинных текстов", ""],
        ["пятница", "3", "Marketing/A-ADS", "Ferrari Goes Crypto", ""],
        ["суббота", "4", "", "", ""],
    ]
    items, _, unparsed = plans.parse_sheet("A-ADS", "ноябрь", rows, today=D(2026, 11, 1))
    assert [(i.ctype, i.pub_date) for i in items] == [(POST, D(2026, 11, 3))]
    assert unparsed == []


def test_plan_name_filter():
    hit = ["Контент-план_Флобериум", "LE: контент-план", "Igaming КП", "Пункт Б | Контент-план"]
    miss = ["Медиаплан", "Брифинг", "Отчёт по клипам", "Компания"]  # "компания" не "кп"
    for name in hit:
        assert plans.PLAN_NAME_RE.search(name), name
    for name in miss:
        assert not plans.PLAN_NAME_RE.search(name), name


def test_chunks_dont_split_html_tags():
    """Регресс: резать по числу символов ломало Telegram-разметку, разрез мог попасть
    внутрь <b>. _chunks пакует список готовых блоков, ни один блок не режет —
    даже многострочный <blockquote expandable> с "\\n" внутри остаётся целым."""
    blocks = [f"<b>строка {i}</b> — " + "x" * 40 for i in range(50)]
    chunks = bot._chunks(blocks, limit=200)
    assert len(chunks) > 1  # реально режется на несколько сообщений
    for c in chunks:
        assert c.count("<b>") == c.count("</b>")
    assert "\n".join(chunks) == "\n".join(blocks)  # ничего не потеряно и не задвоено


def test_chunks_keep_blockquote_atomic():
    """Многострочная цитата — один блок в списке, её нельзя разорвать между сообщениями,
    даже когда она сама длиннее лимита и вместе с соседями точно не влезает."""
    quote = "<blockquote expandable>" + "\n".join(f"строка {i}" for i in range(30)) + "</blockquote>"
    blocks = ["<b>Проект</b>", quote, "<b>Другой проект</b>", "<blockquote expandable>x</blockquote>"]
    chunks = bot._chunks(blocks, limit=50)
    assert any(quote in c for c in chunks)  # цитата целиком внутри какого-то одного сообщения
    for c in chunks:
        assert c.count("<blockquote") == c.count("</blockquote>")


def test_on_error_replies():
    """Исключение в любом хендлере не должно тихо теряться — on_error() ловит его
    централизованно (aiogram @dp.errors()) и отвечает в чат, откуда пришла команда."""
    import asyncio
    from types import SimpleNamespace

    answered = []

    async def fake_answer(text):
        answered.append(text)

    event = SimpleNamespace(
        exception=RuntimeError("boom"),
        update=SimpleNamespace(message=SimpleNamespace(answer=fake_answer)),
    )
    asyncio.run(bot.on_error(event))
    assert answered and "Не получилось" in answered[0]


def test_calendar_slots():
    """Настоящие названия событий из агентского календаря."""
    def ev(day, summary, allday=False, span=1):
        if allday:  # end.date у Google эксклюзивный
            start, end = f"2026-08-{day:02d}", f"2026-08-{day + span:02d}"
            return {"start": {"date": start}, "end": {"date": end}, "summary": summary}
        return {"start": {"dateTime": f"2026-08-{day:02d}T17:15:00+03:00"}, "summary": summary}

    events = [
        ev(10, "Антон может"),
        ev(10, "Ульяна может"),
        ev(10, "Съемка Крашено: Антон"),          # Антон в этот день уже занят
        ev(11, "Александр может", allday=True, span=3),  # плашка на 11-13 разом
        ev(11, "Планерка ХС "),                   # не слот
        ev(11, "Созвон по сценариям дроны 09:00 МСК"),
        ev(12, "Лены не будет"),                  # не слот
        ev(12, "Аудит Надя Плетнева тг"),
        ev(13, "Ульяна может"),
        ev(13, "Ника съемка: Ульяна"),            # обратный порядок слов, тоже бронь
        ev(14, "Александр свободен", allday=True), # второе слово для «свободен»
    ]
    assert plans.classify(events) == {
        D(2026, 8, 10): ["Ульяна"],
        D(2026, 8, 11): ["Александр"],
        D(2026, 8, 12): ["Александр"],
        D(2026, 8, 13): ["Александр"],            # хвост плашки 11-13, Ульяна занята съёмкой
        D(2026, 8, 14): ["Александр"],
    }


def test_parse_date():
    assert plans.parse_date("2026-05-15") == D(2026, 5, 15)
    assert plans.parse_date("22.09.2025") == D(2025, 9, 22)
    assert plans.parse_date("итого") is None
    # без года берётся ближайший к сегодня, в т.ч. прошлый/следующий
    assert plans.parse_date("12 июня", today=D(2026, 8, 14)) == D(2026, 6, 12)
    assert plans.parse_date("5 января", today=D(2026, 12, 20)) == D(2027, 1, 5)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
