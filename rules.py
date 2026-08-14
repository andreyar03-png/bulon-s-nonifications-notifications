"""Сроки и рабочие дни.

Рабочий день = будний (пн-пт). Праздники агентство работает, производственный
календарь РФ не нужен. Дата выкладки может быть любым днём недели, отсчёт
дедлайнов идёт назад по будним дням от неё.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import NamedTuple

REEL, POST, STORY = "REEL", "POST", "STORY"


class Item(NamedTuple):
    project: str
    sheet: str
    ctype: str
    pub_date: date | None
    topic: str


class Task(NamedTuple):
    date: date
    kind: str
    label: str


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def wd(d: date, n: int) -> date:
    """Сдвиг на n рабочих дней (n<0 — назад). Результат всегда будний день."""
    if n == 0:
        while not is_workday(d):
            d += timedelta(days=1)
        return d
    step = 1 if n > 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if is_workday(d):
            left -= 1
    return d


def next_workday(d: date) -> date:
    return wd(d, 1)


def shoot_date(pub: date) -> date:
    """Базовая дата съёмки: за 5 р.д. до выкладки (гибкость ±1 р.д. — см. suggest_shoots)."""
    return wd(pub, -5)


def tasks(item: Item) -> list[Task]:
    """Все расчётные дедлайны единицы контента. Пустой список, если нет даты выкладки."""
    p = item.pub_date
    if p is None:
        return []
    if item.ctype == REEL:
        s = shoot_date(p)
        return [
            Task(wd(s, -5), "script", "Написать сценарий (2 р.д.)"),
            Task(wd(s, -3), "script_ok", "Дедлайн: сценарий согласован"),
            Task(wd(s, -2), "storyboard", "Раскадровка"),
            Task(s, "shoot", "Съёмка"),
            Task(wd(s, 1), "edit", "Монтаж (2 р.д.)"),
            Task(wd(p, -1), "client", "Отправить клиенту на согласование"),
        ]
    return [
        Task(wd(p, -3), "text", "Написать текст"),
        Task(wd(p, -2), "image", "Сделать картинку"),
        Task(wd(p, -1), "client", "Отправить клиенту на согласование"),
    ]


def edit_end(item: Item) -> date:
    """Последний день монтажа рилса (монтаж = 2 р.д. со следующего дня после съёмки)."""
    return wd(shoot_date(item.pub_date), 2)


SHOOT_BATCH = 5  # за одну съёмку снимается до 5 рилсов — см. "Объёмы" в ТЗ


def suggest_shoots(items, slots, load=None):
    """Подбор дат съёмок: рилсы одного проекта режутся на пачки по SHOOT_BATCH (они
    снимаются одной сессией, не по отдельному слоту на каждый), слот на пачку — окно
    ±1 р.д. вокруг даты самого раннего рилса в ней, только дни со слотами операторов;
    при конкуренции нескольких пачек за одну неделю — распределяем по дням.

    items: элементы REEL с датой выкладки; slots: {date: [подписи слотов]}.
    Возвращает [(пачка item'ов, [даты-кандидаты, лучшая первой])].
    """
    load = dict(load or {})
    by_project = {}
    for it in items:
        by_project.setdefault(it.project, []).append(it)

    out = []
    for proj in sorted(by_project):
        reels = sorted(by_project[proj], key=lambda i: i.pub_date)
        for i in range(0, len(reels), SHOOT_BATCH):
            batch = reels[i:i + SHOOT_BATCH]
            base = shoot_date(batch[0].pub_date)
            window = [wd(base, -1), base, wd(base, 1)]
            cand = [d for d in window if slots.get(d)]
            cand.sort(key=lambda d: (load.get(d, 0), abs((d - base).days)))
            if cand:
                load[cand[0]] = load.get(cand[0], 0) + 1
            out.append((batch, cand))
    return out
