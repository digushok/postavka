"""Расчёт распределения закупки по городам и разбиение на грузоместа.

Чистые функции без обращений к сети — их легко проверять тестами.
"""
import re
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


LAYOUT_RE = re.compile(r"^(\d+)\s*(?:[*xх×]|по)\s*(\d+)$", re.I)


def parse_layout(text):
    """Раскладка по коробкам: «10×24+14+10» = 10 коробок по 24, одна на 14, одна на 10.
    «A×B» (или A*B, AxB, «A по B») — A коробок по B шт; число без × — одна коробка.
    Возвращает [[коробок, шт], …] или None, если это просто число (делится по «В коробке, шт»)."""
    s = str(text or "").strip().replace(" ", "").replace("\u00a0", "")
    if not s or not re.search(r"[+*xх×]|по", s, re.I):
        return None
    out = []
    for part in s.split("+"):
        if not part:
            continue
        m = LAYOUT_RE.match(part)
        if m:
            n, q = int(m.group(1)), int(m.group(2))
        elif part.isdigit():
            n, q = 1, int(part)
        else:
            raise ValueError(f"не понял «{part}» — пишите так: 10×24+14+10 (10 коробок по 24, одна на 14, одна на 10)")
        if n > 0 and q > 0:
            out.append([n, q])
    if not out:
        raise ValueError("в раскладке нет ни одной коробки")
    return out


def layout_total(layout):
    return sum(int(n) * int(q) for n, q in layout or [])


def layout_boxes(layout):
    return [int(q) for n, q in layout for _ in range(int(n))]


def build_boxes(matrix, products, city_order, layouts=None):
    """Строит список грузомест по каждому городу.

    layouts — {cluster_id: {sku: [[коробок, шт], …]}}: раскладка, заданная вручную (используется,
    если её сумма равна количеству в ячейке). Возвращает {cluster_id: [{key, sku, qty}]} — в коробке один SKU.
    """
    info = {str(p["sku"]): p for p in products}
    result = {}
    for cid in city_order:
        cid = str(cid)
        boxes = []
        n = 0
        for p in products:
            sku = str(p["sku"])
            qty = matrix.get(cid, {}).get(sku, 0)
            lay = ((layouts or {}).get(cid) or {}).get(sku)
            qs = layout_boxes(lay) if lay and layout_total(lay) == int(qty or 0) else \
                split_boxes(qty, info[sku].get("box_size"))
            for q in qs:
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
