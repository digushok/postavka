"""Работа с таймслотами: общие слоты для всех поставок и автоматический выбор."""
from datetime import datetime, timedelta


def parse(s):
    """'2026-11-10T09:00:00Z' → datetime (локальное время склада, без зоны)."""
    s = (s or "").replace(" ", "T")
    return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")


def key(slot):
    return f"{slot[0]}|{slot[1]}"


def common_slots(slot_lists):
    """Слоты, доступные во всех списках. slot_lists — список списков [from, to]."""
    lists = [l for l in slot_lists if l is not None]
    if not lists:
        return []
    common = {key(s): s for s in lists[0]}
    for l in lists[1:]:
        ks = {key(s) for s in l}
        common = {k: v for k, v in common.items() if k in ks}
    return sorted(common.values(), key=lambda s: parse(s[0]))


def slot_counts(slot_lists):
    """{key: (slot, сколько поставок могут его взять)} — для экрана выбора."""
    out = {}
    for l in slot_lists:
        for s in l or []:
            k = key(s)
            if k in out:
                out[k][1] += 1
            else:
                out[k] = [s, 1]
    return sorted(out.values(), key=lambda x: parse(x[0][0]))


def _hm(s, default):
    try:
        h, m = (s or default).split(":")
        return int(h), int(m)
    except Exception:
        h, m = default.split(":")
        return int(h), int(m)


def distance(slot, pref):
    """Насколько слот далёк от желаемого диапазона, в минутах (0 — внутри).

    pref: {date_from: 'YYYY-MM-DD', date_to: 'YYYY-MM-DD', time_from: 'HH:MM', time_to: 'HH:MM'}
    """
    start = parse(slot[0])
    d1 = datetime.strptime(pref["date_from"], "%Y-%m-%d")
    d2 = datetime.strptime(pref.get("date_to") or pref["date_from"], "%Y-%m-%d")
    if d2 < d1:
        d1, d2 = d2, d1
    th1, tm1 = _hm(pref.get("time_from"), "00:00")
    th2, tm2 = _hm(pref.get("time_to"), "23:59")
    best = None
    day = d1 - timedelta(days=0)
    while day <= d2:
        w1 = day.replace(hour=th1, minute=tm1)
        w2 = day.replace(hour=th2, minute=tm2)
        if w2 <= w1:
            w2 = w1 + timedelta(minutes=1)
        # слот подходит, если его начало попадает в желаемое окно
        if w1 <= start <= w2:
            dist = 0
        elif start < w1:
            dist = (w1 - start).total_seconds() / 60
        else:
            dist = (start - w2).total_seconds() / 60
        best = dist if best is None else min(best, dist)
        day += timedelta(days=1)
    return best if best is not None else 10 ** 9


def pick(slots, pref):
    """Самый ранний слот внутри диапазона, а если такого нет — ближайший к нему."""
    if not slots:
        return None
    return min(slots, key=lambda s: (distance(s, pref), parse(s[0])))


def nearest_to(slots, target):
    """Слот, начало которого ближе всего к началу target."""
    if not slots:
        return None
    t = parse(target[0])

    def cost(s):
        d = (parse(s[0]) - t).total_seconds()
        return d if d >= 0 else -d * 3      # сдвиг на более ранний срок — в 3 раза «дороже»
    return min(slots, key=lambda s: (cost(s), parse(s[0])))


def human(slot):
    a, b = parse(slot[0]), parse(slot[1])
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return f"{a:%d.%m} {days[a.weekday()]} {a:%H:%M}–{b:%H:%M}"
