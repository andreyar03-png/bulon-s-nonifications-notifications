"""SQLite: суточный кэш контент-планов, кэш LLM-разбора, лог отправленных напоминаний."""
import os
import sqlite3
from pathlib import Path

PATH = Path(os.getenv("DB_PATH", "data/bot.db"))


def _c():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(PATH)
    c.executescript(
        "create table if not exists cache(id integer primary key check(id=1), day text, payload blob);"
        "create table if not exists llm(raw text primary key, ctype text);"
        "create table if not exists sent(key text primary key, at text default current_timestamp);"
        "create table if not exists settings(key text primary key, value text);"
    )
    return c


def cache_get():
    """(day, payload) или None."""
    with _c() as c:
        return c.execute("select day, payload from cache where id=1").fetchone()


def cache_set(day, payload):
    with _c() as c:
        c.execute("insert or replace into cache(id, day, payload) values (1,?,?)", (day, payload))


def llm_get(raw):
    """Кэшированный тип, "" если LLM не распознала, None если запроса не было."""
    with _c() as c:
        row = c.execute("select ctype from llm where raw=?", (raw,)).fetchone()
    return row[0] if row else None


def llm_set(raw, ctype):
    with _c() as c:
        c.execute("insert or replace into llm(raw, ctype) values (?,?)", (raw, ctype or ""))


def mark_sent(key) -> bool:
    """True — отправляем впервые; False — уже отправляли (перезапуск бота, дубль)."""
    with _c() as c:
        return c.execute("insert or ignore into sent(key) values (?)", (key,)).rowcount == 1


def setting_get(key, default=None):
    with _c() as c:
        row = c.execute("select value from settings where key=?", (key,)).fetchone()
    return row[0] if row else default


def setting_set(key, value):
    with _c() as c:
        c.execute("insert or replace into settings(key, value) values (?,?)", (key, str(value)))
