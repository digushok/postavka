"""Расчёт распределения закупки по городам и разбиение на грузоместа.

Чистые функции без обращений к сети — их легко проверять тестами.
"""
from fractions import Fraction

METHOD_REMAINDER_CITY = "remainder_city"   # весь остаток в один город
METHOD_FAIR = "fair"                       # по наибольшему остатку
METHOD_FLOOR = "floor"                     # округлить вниз, остаток не распределять

MAX_BOXES_PER_REQUEST = 30   # /v1/cargoes/create принимает не больше 30 коробок за раз


def _frac(x):
    return Fraction(str(x)) if x not in (None, "") else Fraction(0)


def distribute(products, cities, method=METHOD_REMAINDER_CITY, remainder_city=None):
    """Распределяет количество каждого товара по городам.

    products: [{sku, qty, box_size, multiple}]
    cities:   [{cluster_id, share}]  (доли в любых единицах, нормируются)
    remainder_city: cluster_id города для остатка (по умолчанию — с наибольшей долей)

    Если у товара задан box_size и multiple=True, делим целыми коробками,
    а «рассыпные» штуки (не набравшие коробку) отправляем в город для остатка.

    Возвращает (matrix, unassigned):
      matrix[cluster_id][sku] = количество,
      unassigned[sku] = нераспределённое количество (только для METHOD_FLOOR).
    """
    shares = [_frac(c.get("share")) for c in cities]
    total_share = sum(shares)
    matrix = {str(c["cluster_id"]): {} for c in cities}
    unassigned = {}
    if not cities:
        return matrix, {str(p["sku"]): int(p.get("qty") or 0) for p in products}

    ids = [str(c["cluster_id"]) for c in cities]
    if remainder_city is None or str(remainder_city) not in ids:
        best = max(range(len(cities)), key=lambda i: (shares[i], -i))
        rem_id = ids[best]
    else:
        rem_id = str(remainder_city)

    for p in products:
        sku = str(p["sku"])
        qty = int(p.get("qty") or 0)
        box = int(p.get("box_size") or 0)
        unit = box if (box > 0 and p.get("multiple")) else 1
        units_total, loose = divmod(qty, unit)

        if total_share <= 0:
            for cid in ids:
                matrix[cid][sku] = 0
            unassigned[sku] = qty
            continue

        exact = [units_total * s / total_share for s in shares]
        base = [int(e) for e in exact]          # floor для неотрицательных
        rem_units = units_total - sum(base)

        if method == METHOD_FAIR and rem_units:
            order = sorted(range(len(cities)),
                           key=lambda i: (-(exact[i] - base[i]), -shares[i], i))
            for i in order[:rem_units]:
                base[i] += 1
            rem_units = 0
        elif method == METHOD_REMAINDER_CITY and rem_units:
            base[ids.index(rem_id)] += rem_units
            rem_units = 0

        for i, cid in enumerate(ids):
            matrix[cid][sku] = base[i] * unit

        left = rem_units * unit + loose
        if left and method != METHOD_FLOOR:
            matrix[rem_id][sku] += left
            left = 0
        if left:
            unassigned[sku] = left
    return matrix, unassigned


def split_boxes(qty, box_size):
    """Разбивает количество на коробки: полные по box_size + одна с остатком.
    Без box_size — всё в одну коробку. Возвращает список количеств."""
    qty = int(qty or 0)
    box = int(box_size or 0)
    if qty <= 0:
        return []
    if box <= 0:
        return [qty]
    full, rest = divmod(qty, box)
    return [box] * full + ([rest] if rest else [])


def build_boxes(matrix, products, city_order):
    """Строит список грузомест по каждому городу.

    Возвращает {cluster_id: [{key, sku, qty}]} — в каждой коробке один SKU.
    """
    info = {str(p["sku"]): p for p in products}
    result = {}
    for cid in city_order:
        cid = str(cid)
        boxes = []
        n = 0
        for p in products:
            sku = str(p["sku"])
            for q in split_boxes(matrix.get(cid, {}).get(sku, 0), info[sku].get("box_size")):
                n += 1
                boxes.append({"key": f"b{n:03d}-{sku}-{q}", "sku": sku, "qty": q})
        result[cid] = boxes
    return result


def group_boxes(boxes_by_city, products, city_order, city_names=None):
    """Группирует одинаковые по составу коробки (SKU + количество).

    Возвращает список групп в порядке товаров, затем по убыванию количества в коробке:
      {id, sku, qty, name, total, cities: [{cluster_id, name, count}]}
    """
    city_names = city_names or {}
    pindex = {str(p["sku"]): i for i, p in enumerate(products)}
    pname = {str(p["sku"]): (p.get("short") or p.get("name") or str(p["sku"])) for p in products}
    groups = {}
    for cid in city_order:
        cid = str(cid)
        for b in boxes_by_city.get(cid, []):
            k = (b["sku"], b["qty"])
            g = groups.setdefault(k, {"sku": b["sku"], "qty": b["qty"], "name": pname.get(b["sku"], b["sku"]),
                                      "total": 0, "cities": {}})
            g["total"] += 1
            g["cities"][cid] = g["cities"].get(cid, 0) + 1
    out = []
    for (sku, qty), g in sorted(groups.items(), key=lambda kv: (pindex.get(kv[0][0], 9999), -kv[0][1])):
        out.append({
            "id": f"{sku}x{qty}",
            "sku": sku, "qty": qty, "name": g["name"], "total": g["total"],
            "cities": [{"cluster_id": c, "name": city_names.get(c, c), "count": n} for c, n in g["cities"].items()],
        })
    return out


def chunks(lst, size=MAX_BOXES_PER_REQUEST):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# ================================================================ НАБОРЫ
# Набор — это группа грузомест, по одному на каждый SKU (в каждом грузоместе один товар).
# Кластер получает целое число наборов. Одинаковые наборы во всех кластерах дают много
# одинаковых грузомест — их этикетки собираются в один файл, клеить удобно.

KIT_NAMES = ["А", "Б", "В", "Г", "Д", "Е", "Ж", "З"]


def round_shares_to_steps(rows, step=2.5):
    """Доли → целое число шагов, всего ровно 100 / step шагов (метод наибольшего остатка)."""
    total_steps = int(round(100 / step))
    raw = {str(r["cluster_id"]): float(r.get("share") or 0) / step for r in rows}
    s = sum(raw.values())
    if s <= 0:
        return {k: 0 for k in raw}
    raw = {k: v * total_steps / s for k, v in raw.items()}           # нормируем к 100%
    base = {k: int(v) for k, v in raw.items()}
    left = total_steps - sum(base.values())
    order = sorted(raw, key=lambda k: (-(raw[k] - base[k]), -raw[k]))
    for k in order[:left]:
        base[k] += 1
    return base


def auto_kits(products, rows, step=2.5, big_steps=2, remainder_cluster=None):
    """Автоподбор наборов из долей.

    Малый набор (Б) = закупка × шаг, большой (А) = закупка × (шаг × big_steps) по каждому SKU.
    Кластер с долей k шагов получает k // big_steps наборов А и k % big_steps наборов Б.
    Остаток от округления уходит в набор В для кластера остатка (вместо одного его набора А).
    """
    steps = round_shares_to_steps(rows, step)
    skus = [str(p["sku"]) for p in products]
    qty = {str(p["sku"]): int(p.get("qty") or 0) for p in products}
    big = {s: int(Fraction(qty[s]) * Fraction(str(step)) * big_steps / 100 + Fraction(1, 2)) for s in skus}
    small = {s: int(Fraction(qty[s]) * Fraction(str(step)) / 100) for s in skus}
    alloc = {}
    for r in rows:
        cid = str(r["cluster_id"])
        k = steps.get(cid, 0)
        alloc[cid] = {"А": k // big_steps, "Б": k % big_steps}
    ids = [str(r["cluster_id"]) for r in rows]
    if remainder_cluster is None or str(remainder_cluster) not in ids:
        remainder_cluster = max(ids, key=lambda c: (steps.get(c, 0), -ids.index(c))) if ids else None
    rem_c = str(remainder_cluster) if remainder_cluster is not None else None

    used = {s: sum(alloc[c]["А"] * big[s] + alloc[c]["Б"] * small[s] for c in alloc) for s in skus}
    rest = {s: qty[s] - used[s] for s in skus}
    defs = [{"id": "А", "items": big}, {"id": "Б", "items": small}]
    warn = []
    if rem_c and any(rest.values()):
        if alloc[rem_c]["А"] > 0:
            alloc[rem_c]["А"] -= 1
            v = {s: big[s] + rest[s] for s in skus}
        elif alloc[rem_c]["Б"] > 0:
            alloc[rem_c]["Б"] -= 1
            v = {s: small[s] + rest[s] for s in skus}
        else:
            v = dict(rest)
        neg = [s for s in skus if v[s] < 0]
        if neg:
            warn.append("Наборов получилось больше закупки по SKU " + ", ".join(neg) + " — уменьшите доли или наборы")
            v = {s: max(0, v[s]) for s in skus}
        defs.append({"id": "В", "items": v})
        alloc[rem_c]["В"] = 1
    shares_exact = {str(r["cluster_id"]): float(r.get("share") or 0) for r in rows}
    total_share = sum(shares_exact.values()) or 100
    deviation = {c: round(steps[c] * step - shares_exact[c] * 100 / total_share, 2) for c in steps}
    return {"defs": defs, "alloc": alloc, "steps": steps, "step": step, "deviation": deviation,
            "remainder_cluster": rem_c, "warn": warn}


def kits_matrix(defs, alloc, products):
    """Сколько штук каждого SKU получает каждый кластер при данных наборах."""
    items = {d["id"]: d.get("items") or {} for d in defs}
    out = {}
    for cid, counts in alloc.items():
        out[cid] = {}
        for p in products:
            sku = str(p["sku"])
            out[cid][sku] = sum(int(n or 0) * int(items.get(k, {}).get(sku) or 0) for k, n in counts.items())
    return out


def build_boxes_kits(defs, alloc, products, city_order):
    """Грузоместа по наборам: каждый набор — по грузоместу на SKU (с учётом «В коробке, шт»)."""
    info = {str(p["sku"]): p for p in products}
    items = {d["id"]: d.get("items") or {} for d in defs}
    result = {}
    for cid in city_order:
        cid = str(cid)
        boxes, n = [], 0
        for d in defs:
            for _ in range(int((alloc.get(cid) or {}).get(d["id"]) or 0)):
                for p in products:
                    sku = str(p["sku"])
                    for q in split_boxes(items[d["id"]].get(sku), info[sku].get("box_size")):
                        n += 1
                        boxes.append({"key": f"b{n:03d}-{sku}-{q}", "sku": sku, "qty": q, "kit": d["id"]})
        result[cid] = boxes
    return result
