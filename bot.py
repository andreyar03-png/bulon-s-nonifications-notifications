"""Telegram-бот-напоминалка (Бульон). Вечерний джоб пн-пт + пятничный отчёт 12:00 МСК."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
import plans
import rules

MSK = ZoneInfo("Europe/Moscow")
CHAT_ID = int(os.environ["CHAT_ID"])
THREAD_ID = int(os.environ["THREAD_ID"]) if os.getenv("THREAD_ID") else None
# /time меняет час на лету и переживает рестарт — БД, если задавали, иначе .env, иначе 19
DAILY_HOUR = int(db.setting_get("daily_hour") or os.getenv("DAILY_HOUR", "19"))
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
TYPE_RU = {rules.REEL: "Рилс", rules.POST: "Пост", rules.STORY: "Сторис"}

bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
sched: AsyncIOScheduler | None = None  # создаётся в main(), нужен /time для reschedule_job


def today() -> dt.date:
    return dt.datetime.now(MSK).date()


def e(s) -> str:
    return html.escape(str(s))


def d_ru(d: dt.date) -> str:
    return f"{d:%d.%m} ({DAYS[d.weekday()]})"


def line(item: rules.Item, prefix="") -> str:
    """Без имени проекта — оно теперь общий заголовок группы, см. grouped()."""
    topic = e(item.topic) or "без темы"
    return f"• {prefix}{TYPE_RU[item.ctype]}: {topic}"


def shoot_line(batch, cand, slots) -> str:
    """batch — до 5 рилсов одного проекта, снимаются одной сессией; cand — даты-кандидаты
    (лучшая первой) из rules.suggest_shoots. Формат: 'Назначить съёмки, доступные слоты: ...'."""
    topics = "; ".join(e(i.topic) or "без темы" for i in batch)
    if not cand:
        base = rules.shoot_date(batch[0].pub_date)
        return f"• Нет слотов на {base:%d.%m} ±1 — рилсов {len(batch)}: {topics}"
    opts = ", ".join(f"{d:%d.%m} ({e(', '.join(slots[d]))})" for d in cand)
    return f"• Назначить съёмки, доступные слоты: {opts} — рилсов {len(batch)}: {topics}"


def grouped(pairs: list[tuple[str, str]]) -> list[str]:
    """[(проект, строка)] -> жирный заголовок проекта + сворачиваемая цитата со всеми
    его строками. Порядок проектов — по первому появлению. Каждый элемент результата
    самодостаточен (весь HTML внутри открыт/закрыт), это важно для чанкинга ниже."""
    order, by_proj = [], {}
    for proj, txt in pairs:
        if proj not in by_proj:
            order.append(proj)
            by_proj[proj] = []
        by_proj[proj].append(txt)
    out = []
    for proj in order:
        out.append(f"<b>{e(proj)}</b>")
        out.append("<blockquote expandable>" + "\n".join(by_proj[proj]) + "</blockquote>")
    return out


# ---------- тексты ----------
# Каждая *_text функция возвращает список "блоков" — атомарных кусков HTML (одна
# строка или целая цитата), которые нельзя резать посередине. Собирает их в
# сообщения только send()/reply(), см. ниже.

def _todo_lines(target: dt.date, items, slots) -> list[str]:
    """"Съёмка" — особая задача: без предложенных слотов операторов она не actionable,
    поэтому рендерится через ту же батчевую логику, что и /shoots, а не как обычный
    пункт списка."""
    todo = sorted(
        ((t, i) for i in items for t in rules.tasks(i) if t.date == target),
        key=lambda x: (x[1].project, x[0].kind),
    )
    shoots = [i for t, i in todo if t.kind == "shoot"]
    pairs = [(i.project, line(i, f"{t.label} — ")) for t, i in todo if t.kind != "shoot"]
    pairs += [(batch[0].project, shoot_line(batch, cand, slots))
              for batch, cand in rules.suggest_shoots(shoots, slots)]
    return grouped(pairs) if pairs else ["— пусто"]


def _pub_lines(days: list[dt.date], items) -> list[str]:
    pub = [(d, i) for d in days for i in items if i.pub_date == d]
    if not pub:
        return ["— пусто"]
    pairs = [(i.project, line(i, f"{d_ru(d)} — " if len(days) > 1 else "")) for d, i in pub]
    return grouped(pairs)


def _warnings(undated, unparsed) -> list[str]:
    out = []
    if undated:
        pairs = [(i.project, line(i)) for i in undated]
        out += ["", "<b>⚠️ Контент без даты выкладки</b>"] + grouped(pairs[:15])
        if len(pairs) > 15:
            out.append(f"…и ещё {len(pairs) - 15}")
    if unparsed:
        out += ["", "<b>⚠️ Нераспознанные строки</b>"] + cap([f"• {e(s)}" for s in unparsed])
    return out


def daily_text(now: dt.date, items, undated, unparsed, slots) -> list[str]:
    """Вечерний джоб: план на следующий рабочий день + что публикуется завтра
    (в пятницу — сразу и сб/вс, иначе их не покажет ни одно сообщение)."""
    target = rules.next_workday(now)
    pub_days = [now + dt.timedelta(days=1)]
    if now.weekday() == 4:
        pub_days += [now + dt.timedelta(days=2), now + dt.timedelta(days=3)]

    out = [f"<b>🗓 План на {d_ru(target)}</b>", "", "<b>1. ✅ Поставить в работу</b>"]
    out += _todo_lines(target, items, slots)
    out += ["", "<b>2. 📤 Выходит</b>"] + _pub_lines(pub_days, items)
    out += _warnings(undated, unparsed)
    return out


def summary_text(target: dt.date, items, undated, unparsed, slots) -> list[str]:
    """Сводка по конкретному дню (/today, /tomorrow, /date) — без пятничной логики
    выходных, просто что по плану на этот день, задачи + публикации."""
    out = [f"<b>🗓 Сводка на {d_ru(target)}</b>", "", "<b>✅ Поставить в работу</b>"]
    out += _todo_lines(target, items, slots)
    out += ["", "<b>📤 Выходит</b>"] + _pub_lines([target], items)
    out += _warnings(undated, unparsed)
    return out


def cap(rows, n=15):
    """Старых листов в планах много — предупреждения не должны съедать всё сообщение."""
    return rows[:n] + ([f"…и ещё {len(rows) - n}"] if len(rows) > n else [])


def week_bounds(now: dt.date):
    mon = now - dt.timedelta(days=now.weekday())
    return mon, mon + dt.timedelta(days=6)


def report_text(now: dt.date, items, slots) -> list[str]:
    mon, sun = week_bounds(now)
    in_week = lambda d: d and mon <= d <= sun
    task_day = lambda i, kind: next((t.date for t in rules.tasks(i) if t.kind == kind), None)
    reels = [i for i in items if i.ctype == rules.REEL]

    def block(title, pairs):
        return [f"<b>{title}</b>"] + (grouped(pairs) if pairs else ["— пусто"]) + [""]

    out = [f"<b>📊 Отчёт за неделю {mon:%d.%m}–{sun:%d.%m}</b>", ""]
    out += block("Сценарии на этой неделе",
                 [(i.project, line(i, f"{task_day(i,'script'):%d.%m} — ")) for i in reels if in_week(task_day(i, "script"))])
    out += block("Посты/сторис на этой неделе",
                 [(i.project, line(i, f"{task_day(i,'text'):%d.%m} — ")) for i in items
                  if i.ctype != rules.REEL and in_week(task_day(i, "text"))])

    shoots = [(rules.shoot_date(i.pub_date), i) for i in reels if in_week(rules.shoot_date(i.pub_date))]
    out += block("Съёмки проведённые", [(i.project, line(i, f"{d:%d.%m} — ")) for d, i in shoots if d <= now])
    out += block("Съёмки запланированные", [(i.project, line(i, f"{d:%d.%m} — ")) for d, i in shoots if d > now])
    out += block("Рилсы смонтированные",
                 [(i.project, line(i)) for i in reels if in_week(rules.edit_end(i)) and rules.edit_end(i) <= now])
    out += block("Рилсы в работе (монтаж)",
                 [(i.project, line(i)) for i in reels
                  if task_day(i, "edit") <= now < rules.edit_end(i)])
    out += block("Выложено за неделю",
                 [(i.project, line(i, f"{i.pub_date:%d.%m} — ")) for i in items if in_week(i.pub_date) and i.pub_date <= now])

    nxt = [i for i in reels if now < rules.shoot_date(i.pub_date) <= sun + dt.timedelta(days=7)]
    pairs = [(batch[0].project, shoot_line(batch, cand, slots))
             for batch, cand in rules.suggest_shoots(nxt, slots)]
    out += block("Предлагаемые даты съёмок", pairs)
    while out and out[-1] == "":
        out.pop()
    return out


# ---------- отправка ----------

def _chunks(blocks: list[str], limit: int = 3800) -> list[str]:
    """Пакует атомарные блоки в сообщения по лимиту символов, не разрезая ни один
    блок — иначе разрез может попасть внутрь HTML-тега (<b>, <blockquote>...) и
    Telegram отклонит сообщение целиком."""
    out, cur, cur_len = [], [], 0
    for b in blocks:
        add = len(b) + (1 if cur else 0)
        if cur and cur_len + add > limit:
            out.append("\n".join(cur))
            cur, cur_len = [b], len(b)
        else:
            cur.append(b)
            cur_len += add
    if cur:
        out.append("\n".join(cur))
    return out


async def send(blocks: list[str]):
    for chunk in _chunks(blocks):
        await bot.send_message(CHAT_ID, chunk, message_thread_id=THREAD_ID)


async def reply(m: Message, blocks: list[str]):
    for chunk in _chunks(blocks):
        await m.answer(chunk)


async def slots_from(d: dt.date):
    """Слоты операторов на 3 недели вперёд от d — с запасом на батчинг съёмок."""
    return await asyncio.to_thread(plans.slots, d, d + dt.timedelta(days=21))


async def daily_job():
    now = today()
    if not rules.is_workday(now) or not db.mark_sent(f"daily:{now}"):
        return
    items, undated, unparsed = await asyncio.to_thread(plans.load)
    sl = await slots_from(now)
    await send(daily_text(now, items, undated, unparsed, sl))


async def report_job():
    now = today()
    if not db.mark_sent(f"report:{now}"):
        return
    items, _, _ = await asyncio.to_thread(plans.load)
    mon, sun = week_bounds(now)
    sl = await asyncio.to_thread(plans.slots, now, sun + dt.timedelta(days=14))
    await send(report_text(now, items, sl))


# ---------- команды (для ручной проверки) ----------

@dp.message(Command("start", "help"))
async def cmd_help(m: Message):
    thread = f"\nTHREAD_ID: <code>{m.message_thread_id}</code>" if m.message_thread_id else ""
    await m.answer(
        "/today — сводка на сегодня\n/tomorrow — сводка на завтра\n"
        "/date 14.08.2026 — сводка на дату\n/plan — план на след. рабочий день (как вечерний джоб)\n"
        "/report — недельный отчёт\n/shoots — даты съёмок\n"
        "/time 20 — час (МСК) ежедневной рассылки, сейчас " + str(DAILY_HOUR) + "\n"
        "/rescan — пересканировать контент-планы\n"
        f"CHAT_ID: <code>{m.chat.id}</code>{thread}"
    )


@dp.message(Command("today"))
async def cmd_today(m: Message):
    items, undated, unparsed = await asyncio.to_thread(plans.load)
    sl = await slots_from(today())
    await reply(m, summary_text(today(), items, undated, unparsed, sl))


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message):
    tgt = today() + dt.timedelta(days=1)
    items, undated, unparsed = await asyncio.to_thread(plans.load)
    sl = await slots_from(tgt)
    await reply(m, summary_text(tgt, items, undated, unparsed, sl))


@dp.message(Command("date"))
async def cmd_date(m: Message, command: CommandObject):
    target = plans.parse_date(command.args or "", today())
    if target is None:
        await m.answer("Формат: /date 14.08.2026")
        return
    items, undated, unparsed = await asyncio.to_thread(plans.load)
    sl = await slots_from(target)
    await reply(m, summary_text(target, items, undated, unparsed, sl))


@dp.message(Command("time"))
async def cmd_time(m: Message, command: CommandObject):
    global DAILY_HOUR
    if not command.args or not command.args.strip().isdigit() or not 0 <= int(command.args) <= 23:
        await m.answer(f"Формат: /time 20 (час МСК, 0-23). Сейчас: {DAILY_HOUR}")
        return
    DAILY_HOUR = int(command.args)
    db.setting_set("daily_hour", DAILY_HOUR)
    sched.reschedule_job("daily", trigger=CronTrigger(day_of_week="mon-fri", hour=DAILY_HOUR, minute=0))
    await m.answer(f"Готово, ежедневная сводка теперь в {DAILY_HOUR}:00 МСК")


@dp.message(Command("plan"))
async def cmd_plan(m: Message):
    items, undated, unparsed = await asyncio.to_thread(plans.load)
    sl = await slots_from(today())
    await reply(m, daily_text(today(), items, undated, unparsed, sl))


@dp.message(Command("report"))
async def cmd_report(m: Message):
    now = today()
    items, _, _ = await asyncio.to_thread(plans.load)
    sl = await slots_from(now)
    await reply(m, report_text(now, items, sl))


@dp.message(Command("shoots"))
async def cmd_shoots(m: Message):
    now = today()
    items, _, _ = await asyncio.to_thread(plans.load)
    sl = await slots_from(now)
    reels = [i for i in items if i.ctype == rules.REEL and rules.shoot_date(i.pub_date) >= now]
    pairs = [(batch[0].project, shoot_line(batch, cand, sl))
             for batch, cand in rules.suggest_shoots(reels, sl)]
    await reply(m, ["<b>Съёмки</b>"] + (grouped(pairs) if pairs else ["— пусто"]))


@dp.message(Command("rescan"))
async def cmd_rescan(m: Message):
    items, undated, unparsed = await asyncio.to_thread(plans.load, True)
    await m.answer(f"Готово: {len(items)} строк, без даты — {len(undated)}, "
                   f"нераспознано — {len(unparsed)}")


COMMANDS = [
    BotCommand(command="today", description="Сводка на сегодня"),
    BotCommand(command="tomorrow", description="Сводка на завтра"),
    BotCommand(command="date", description="Сводка на дату, напр. 14.08.2026"),
    BotCommand(command="plan", description="План на след. рабочий день (как вечерний джоб)"),
    BotCommand(command="report", description="Недельный отчёт"),
    BotCommand(command="shoots", description="Предлагаемые даты съёмок"),
    BotCommand(command="time", description="Час (МСК) ежедневной рассылки, напр. 20"),
    BotCommand(command="rescan", description="Пересканировать контент-планы"),
    BotCommand(command="help", description="Список команд"),
]


async def main():
    global sched
    logging.basicConfig(level=logging.INFO)
    await bot.set_my_commands(COMMANDS)  # автодополнение "/" в Telegram
    sched = AsyncIOScheduler(timezone=MSK)
    sched.add_job(daily_job, CronTrigger(day_of_week="mon-fri", hour=DAILY_HOUR, minute=0), id="daily")
    sched.add_job(report_job, CronTrigger(day_of_week="fri", hour=12, minute=0), id="report")
    sched.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
