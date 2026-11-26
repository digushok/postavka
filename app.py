"""Поставки Ozon FBO. Запуск: python app.py

Windows (.exe): открывает в браузере http://127.0.0.1:8765, работает только на этом компьютере.
Сервер (Docker): переменная OZON_SUPPLIES_SERVER=1 — слушает HOST:PORT для всех,
без браузера, без поиска старых настроек и без кнопок «Открыть папку» и «Закрыть программу».
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import labels as L
import planner as P
import tls
import storage as ST
import worker as W
from ozon_api import OzonError

ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
from version import CHANGES, RELEASE_DATE, REPO, VERSION
import version as V

SERVER_MODE = os.environ.get("OZON_SUPPLIES_SERVER", "").lower() in ("1", "true", "yes")
COOKIE = "postavka_shop"   # своё имя: на том же адресе сервера могут жить другие программы


def client():
    return W.make_client(ST.get_profile())


def pub_profile(p):
    q = dict(p)
    q["api_key_set"] = bool(p.get("api_key"))
    q["api_key"] = ""
    return q


# ------------------------------------------------------------------ API
def migrate_clusters():
    """Версия 1.0 брала неверные ID кластеров. Один раз сбрасываем их во всех шаблонах —
    города заново привяжутся к кластерам по названию."""
    path = os.path.join(ST.DATA, "profiles.json")
    if not os.path.exists(path):
        return
    d = ST._read(path, {"active": None, "profiles": []})   # сырые данные, без значений по умолчанию
    changed = False
    for p in d["profiles"]:
        ts = p.get("timeslot") or {}
        if "days_to" in ts or "time_to" in ts:          # 1.5.1 и раньше: диапазон → желаемые день и время
            p["timeslot"] = {"mode": "auto", "days_from": ts.get("days_from", 2) or 2,
                             "time_from": ts.get("time_from") or "10:00"}
            changed = True
        if p.get("clusters_v") == 2:
            continue
        for t in p.get("templates") or []:
            for c in t.get("cities") or []:
                c["match"] = c.get("match") or c.get("cluster_name") or c.get("name")
                c["cluster_id"] = None
                c["cluster_name"] = None
        if p.get("remainder_city") not in (None, "largest"):
            p["remainder_city"] = "largest"
        p["clusters_v"] = 2
        changed = True
    if changed:
        ST._write(path, d)


_NOTE = {"v": None, "taken": False}


def _note():
    if not _NOTE["taken"]:
        _NOTE["v"] = ST.take_import_note()
        _NOTE["taken"] = True
    return _NOTE["v"]


def api_export(_):
    """Все магазины и их настройки — для резервной копии или переноса на другой компьютер."""
    d = ST.load_profiles()
    return {"app": "ozon-supplies", "exported": time.strftime("%Y-%m-%d %H:%M"), "profiles": d["profiles"],
            "active": d["active"]}


def api_import(b):
    data = b.get("data") or {}
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles or not all(isinstance(p, dict) and p.get("id") for p in profiles):
        raise OzonError("Это не файл настроек программы «Поставки Ozon»")
    d = ST.load_profiles()
    had_key = any(p.get("api_key") for p in d["profiles"])
    by_id = {p["id"]: i for i, p in enumerate(d["profiles"])}
    for p in profiles:
        if p["id"] in by_id:
            d["profiles"][by_id[p["id"]]] = p
        else:
            d["profiles"].append(p)
    # пустой «Мой магазин», созданный при первом запуске, убираем
    d["profiles"] = [p for p in d["profiles"] if p.get("api_key") or p.get("demo") or p["id"] in {x["id"] for x in profiles}
                     or p.get("client_id") or p.get("products")]
    if not had_key or d["active"] not in {p["id"] for p in d["profiles"]}:
        d["active"] = data.get("active") if data.get("active") in {p["id"] for p in d["profiles"]} else profiles[0]["id"]
    ST.save_profiles(d)
    ST.set_current(d["active"])
    return {"imported": len(profiles)}


def _local_only():
    if SERVER_MODE:
        raise OzonError("На сервере это недоступно")


def api_legacy_find(_):
    _local_only()
    return {"found": [{"path": p, "date": time.strftime("%d.%m.%Y %H:%M", time.localtime(t))}
                      for p, t in ST.find_legacy(max_depth=5, time_limit=20)]}


def api_legacy_import(b):
    _local_only()
    path = b.get("path") or ""
    src = os.path.join(path, "profiles.json")
    if not os.path.isfile(src):
        raise OzonError("В этой папке нет настроек программы")
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    shops = os.path.join(path, "shops")
    if os.path.isdir(shops):                      # партии поставок
        ST.copy_tree(shops, os.path.join(ST.DATA, "shops"))
    api_import({"data": data})                    # магазины добавляются к текущим, а не заменяют их
    return {"ok": True}


def api_state(_):
    d = ST.load_profiles()
    return {"version": VERSION, "release_date": RELEASE_DATE, "repo": REPO,
            "changes": [{"version": v, "date": dt, "items": it} for v, dt, it in CHANGES],
            "active": ST.get_profile()["id"], "data_dir": ST.DATA, "import_note": _note(),
            "server": SERVER_MODE,
            "profiles": [{"id": p["id"], "name": p["name"], "demo": p.get("demo")} for p in d["profiles"]],
            "profile": pub_profile(ST.get_profile())}


def api_profile_save(b):
    p = ST.get_profile()
    new = b.get("profile") or {}
    for k, v in new.items():
        if k in ("id", "api_key_set"):
            continue
        if k == "api_key" and not v:
            continue           # пустое поле — ключ не меняем
        p[k] = v
    ST.save_profile(p)
    return {"profile": pub_profile(p)}


def api_profile_new(b):
    p = ST.new_profile(b.get("name") or "Новый магазин", demo=bool(b.get("demo")))
    if p.get("demo"):
        import mock_ozon
        d0 = mock_ozon.DROPOFFS[0]
        p["dropoff"] = {"warehouse_id": d0[0], "name": d0[1], "warehouse_type": d0[2], "address": d0[3]}
        p["dropoffs"] = [p["dropoff"]]
        p["pass"] = {"enabled": True, "driver_name": "Иванов Иван Иванович", "driver_phone": "+79990000000",
                     "vehicle_model": "ГАЗель", "vehicle_number": "А123ВС777"}
        ST.save_profile(p)
    ST.set_current(p["id"])
    return api_state({})


def api_profile_select(b):
    ST.set_active(b["id"])          # выбор по умолчанию для нового браузера
    ST.set_current(b["id"])         # выбор этого браузера (cookie)
    return api_state({})


def api_profile_delete(b):
    ST.delete_profile(b["id"])
    ST.load_profiles()
    return api_state({})


def api_check(_):
    r = client().post("/v1/roles", {})
    roles = r.get("roles") or []
    methods = sorted({m for x in roles for m in (x.get("methods") or [])})
    need = ["/v1/draft/crossdock/create", "/v2/draft/supply/create", "/v1/cargoes/create",
            "/v1/cargoes-label/create", "/v3/product/info/list"]
    missing = [m for m in need if methods and m not in methods]
    return {"ok": True, "roles": [x.get("name") for x in roles], "missing": missing}


def api_clusters(_):
    """Кластеры для поставок. Нужен именно macrolocal_cluster_id — его ждут методы черновиков.
    Основной источник — /v2/cluster/list (макролокальные кластеры). В /v1/cluster/list поле `id` —
    это другой справочник, его использовать нельзя: поставка уедет не в тот кластер."""
    c = client()
    out = {}
    try:
        r = c.post("/v2/cluster/list", {})
        for x in r.get("result") or []:
            cid = x.get("macrolocal_cluster_id")
            name = (((x.get("data") or {}).get("macrolocal_cluster") or {}).get("name"))
            if cid and int(cid) > 0:
                out[str(cid)] = name or str(cid)
    except OzonError:
        pass
    if not out:
        r = c.post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"})
        for x in r.get("clusters") or []:
            cid = x.get("macrolocal_cluster_id")
            if not cid:
                for lc in x.get("logistic_clusters") or []:
                    if lc.get("macrolocal_cluster_id"):
                        cid = lc["macrolocal_cluster_id"]
                        break
            if cid and int(cid) > 0 and str(cid) not in out:   # без macrolocal_cluster_id — пропускаем
                out[str(cid)] = x.get("name") or str(cid)
    return {"clusters": sorted([{"id": k, "name": v} for k, v in out.items()], key=lambda x: x["name"])}


def _search_variants(q):
    """Варианты запроса точки отгрузки. ПВЗ Озон находит только по полному названию в точности как у него
    («МОСКВА_10704» — заглавными, через «_»), поэтому пробуем и заглавными, и с «_» вместо пробела."""
    q = " ".join((q or "").split())
    out = []
    for v in (q, q.upper()):
        out += [v, v.replace(" ", "_"), v.replace("_", " ")]
    out += re.findall(r"\d{4,}", q)
    return [v for i, v in enumerate(out) if v and v not in out[:i]]


def api_dropoff(b):
    q = b.get("q", "")
    c = W.make_client(ST.get_profile(), log=_search_log)
    found = {}
    for v in _search_variants(q):
        r = c.post("/v1/warehouse/fbo/list", {"filter_by_supply_type": ["CREATE_TYPE_CROSSDOCK"], "search": v})
        for x in r.get("search") or []:
            found.setdefault(x.get("warehouse_id"), _wh(x))
    items = list(found.values())
    ql = q.strip().lower()
    items.sort(key=lambda x: (ql not in (x.get("name") or "").lower(), x.get("name") or ""))
    return {"items": items}


def _search_log(s):
    """Ответы Озона на поиск точек — в search.log (папка данных), чтобы было видно, что он вернул."""
    try:
        with open(os.path.join(ST.DATA, "search.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%d.%m %H:%M:%S ") + s + "\n")
    except OSError:
        pass


def _wh(x):
    return {"warehouse_id": x.get("warehouse_id"), "name": x.get("name"),
            "warehouse_type": x.get("warehouse_type"), "address": x.get("address")}


def api_dropoff_resolve(_):
    """Ищет в Озоне точки из списка «найти и добавить» (СЦ Кавказский, СЦ Строгино и т.п.).
    Однозначно найденные добавляет в избранные, по остальным возвращает варианты для выбора."""
    p = ST.get_profile()
    favs = p.get("dropoffs") or []
    if p.get("dropoff") and not any(f["warehouse_id"] == p["dropoff"]["warehouse_id"] for f in favs):
        favs.insert(0, p["dropoff"])
    pending, left = [], []
    can_call = p.get("demo") or (p.get("client_id") and p.get("api_key"))
    for seed in p.get("dropoff_seeds") or []:
        if not can_call:
            pending.append({"seed": seed, "candidates": [], "error": "нужен API-ключ"})
            left.append(seed)
            continue
        try:
            r = client().post("/v1/warehouse/fbo/list",
                              {"filter_by_supply_type": ["CREATE_TYPE_CROSSDOCK"], "search": seed})
        except OzonError as e:
            pending.append({"seed": seed, "candidates": [], "error": str(e)})
            left.append(seed)
            continue
        found = [_wh(x) for x in r.get("search") or [] if seed.lower() in (x.get("name") or "").lower()]
        sc = [x for x in found if "SORTING_CENTER" in (x.get("warehouse_type") or "")
              or "сц" in (x.get("name") or "").lower()]
        pick = sc if len(sc) == 1 else found if len(found) == 1 else None
        if pick:
            if not any(f["warehouse_id"] == pick[0]["warehouse_id"] for f in favs):
                favs.append(pick[0])
        else:
            pending.append({"seed": seed, "error": None,
                            "candidates": sc or found or [_wh(x) for x in r.get("search") or []]})
            left.append(seed)
    p["dropoffs"] = favs
    p["dropoff_seeds"] = left
    if not p.get("dropoff") and favs:
        p["dropoff"] = favs[0]
    ST.save_profile(p)
    return {"profile": pub_profile(p), "pending": pending}


def api_dropoff_fav(b):
    """Добавить / убрать избранную точку, сделать основной, отказаться от поиска."""
    p = ST.get_profile()
    favs = p.get("dropoffs") or []
    act, x = b.get("action"), b.get("item")
    if act == "add" and x and not any(f["warehouse_id"] == x["warehouse_id"] for f in favs):
        favs.append(_wh(x))
    if act in ("add", "default") and b.get("make_default"):
        p["dropoff"] = _wh(x)
    if act == "default":
        p["dropoff"] = _wh(x)
    if act == "remove":
        favs = [f for f in favs if f["warehouse_id"] != x["warehouse_id"]]
        if p.get("dropoff") and p["dropoff"]["warehouse_id"] == x["warehouse_id"]:
            p["dropoff"] = favs[0] if favs else None
    if b.get("seed"):
        p["dropoff_seeds"] = [s for s in p.get("dropoff_seeds") or [] if s != b["seed"]]
    p["dropoffs"] = favs
    ST.save_profile(p)
    return {"profile": pub_profile(p)}


def lookup_products(skus):
    skus = [str(s).strip() for s in skus if str(s).strip().isdigit()]
    if not skus:
        return {}
    out = {}
    c = client()
    for i in range(0, len(skus), 100):
        part = skus[i:i + 100]
        r = c.post("/v3/product/info/list", {"sku": [int(s) for s in part]})
        for it in r.get("items") or []:
            ids = {str(it.get("sku") or "")} | {str(x.get("sku")) for x in it.get("sources") or [] if x.get("sku")}
            for s in part:
                if s in ids:
                    out[s] = {"offer_id": it.get("offer_id"), "name": it.get("name"),
                              "barcode": (it.get("barcodes") or [None])[0]}
    return out


def api_products(b):
    found = lookup_products(b.get("skus") or [])
    p = ST.get_profile()
    for sku, info in found.items():
        cur = p["products"].get(sku, {})
        cur.update({"offer_id": info["offer_id"], "name": info["name"]})
        p["products"][sku] = cur
    ST.save_profile(p)
    return {"found": found, "missing": [s for s in b.get("skus") or [] if str(s) not in found]}


def _plan_products(b):
    return [{"sku": str(x["sku"]), "qty": int(x.get("qty") or 0), "box_size": int(x.get("box_size") or 0) or None,
             "multiple": bool(x.get("multiple")), "short": x.get("short"), "name": x.get("name")}
            for x in b.get("products") or [] if str(x.get("sku") or "").strip()]


def api_plan(b):
    """Расчёт таблицы «Новая поставка»: количество по кластерам и грузоместа.

    Количество из долей; строки с ручными количествами (fixed) берутся как есть."""
    products = _plan_products(b)
    rows = [{"cluster_id": str(r["key"]), "share": r.get("share") or 0, "name": r.get("name"),
             "fixed": r.get("fixed")} for r in b.get("rows") or []]
    order = [r["cluster_id"] for r in rows]
    names = {r["cluster_id"]: r["name"] for r in rows}
    rem = b.get("remainder_city")
    calc, unassigned = P.distribute(products, rows, b.get("method") or P.METHOD_REMAINDER_CITY,
                                    None if rem in (None, "", "largest") else rem)
    matrix = {}
    for r in rows:
        fixed = r.get("fixed")
        matrix[r["cluster_id"]] = ({p["sku"]: max(0, int((fixed or {}).get(p["sku"]) or 0)) for p in products}
                                   if fixed else calc[r["cluster_id"]])
    boxes = P.build_boxes(matrix, products, order)
    groups = P.group_boxes(boxes, products, order, names)
    return {"matrix": matrix, "calc": calc, "unassigned": unassigned, "groups": groups,
            "boxes": {k: len(v) for k, v in boxes.items()}}


def api_batch_create(b):
    p = ST.get_profile()
    products = []
    for x in b["products"]:
        sku = str(x["sku"]).strip()
        saved = p["products"].get(sku, {})
        products.append({"sku": sku, "qty": int(x.get("qty") or 0),
                         "name": x.get("name") or saved.get("name") or sku,
                         "short": x.get("short") or saved.get("short") or "",
                         "offer_id": saved.get("offer_id") or x.get("offer_id"),
                         "box_size": int(x.get("box_size") or 0) or None})
    missing = [x["sku"] for x in products if not x["offer_id"]]
    if missing:
        found = lookup_products(missing)
        for x in products:
            if not x["offer_id"] and x["sku"] in found:
                x["offer_id"] = found[x["sku"]]["offer_id"]
        missing = [x["sku"] for x in products if not x["offer_id"]]
        if missing:
            raise OzonError("Озон не нашёл товары с SKU: " + ", ".join(missing))
    # запоминаем товары (название, короткое имя, размер коробки) для следующих поставок
    for x in products:
        cur = p["products"].setdefault(x["sku"], {})
        cur.update({k: v for k, v in (("offer_id", x["offer_id"]), ("short", x["short"]),
                                      ("box_size", x["box_size"])) if v})
        if x["name"] != x["sku"]:
            cur.setdefault("name", x["name"])
    ST.save_profile(p)
    if not p.get("dropoff") and not b.get("dropoff"):
        raise OzonError("Выберите точку отгрузки")
    cities = [c for c in b["cities"] if c.get("cluster_id")]
    order = [str(c["cluster_id"]) for c in cities]
    matrix = {str(k): {str(s): int(q or 0) for s, q in v.items()} for k, v in b["matrix"].items()}
    boxes = P.build_boxes(matrix, products, order)
    shipments = []
    for c in cities:
        cid = str(c["cluster_id"])
        items = {s: q for s, q in matrix.get(cid, {}).items() if q > 0}
        if not items:
            continue
        units = sum(items.values())
        if units > 5000:
            raise OzonError(f"{c['name']}: {units} шт — Озон принимает не больше 5000 шт в одной поставке")
        shipments.append({"cluster_id": cid, "name": c["name"], "cluster_name": c.get("cluster_name"),
                          "items": items, "boxes": boxes[cid], "status": "new", "error": None, "warn": []})
    if not shipments:
        raise OzonError("Нет ни одной поставки с товаром")
    if b.get("multi") and len(shipments) > 20:
        raise OzonError("Одной заявкой Озон принимает не больше 20 кластеров")
    ids = [s["cluster_id"] for s in shipments]
    if len(set(ids)) != len(ids):
        raise OzonError("Один и тот же кластер выбран для двух городов")
    bid = ST.new_batch_id()
    names = {s["cluster_id"]: s["name"] for s in shipments}
    batch = {
        "id": bid, "created": time.strftime("%d.%m.%Y %H:%M"), "title": b.get("title") or "",
        "mode": b.get("mode", "manual"), "slot_pref": b.get("slot_pref"), "multi": bool(b.get("multi")),
        "dropoff": b.get("dropoff") or p["dropoff"],
        "pass": b.get("pass") if b.get("pass") is not None else p.get("pass"),
        "products": products, "shipments": shipments, "phase": "new", "chosen_slot": None,
        "groups": P.group_boxes({s["cluster_id"]: s["boxes"] for s in shipments}, products,
                                [s["cluster_id"] for s in shipments], names),
        "log": [], "files": None, "demo": bool(p.get("demo")),
        "source": b.get("source"),
    }
    os.makedirs(ST.batch_dir(p["id"], bid), exist_ok=True)
    ST.save_batch(p["id"], batch)
    W.start(p["id"], bid)
    return {"id": bid}


def _batch_view(pid, b):
    b = dict(b)
    b["running"] = W.is_running(b["id"])
    b["labels_done"] = ST.load_labels_done(pid, b["id"])
    b["barcodes"] = _barcodes_view(pid, b)
    return b


def api_batches(_):
    pid = ST.get_profile()["id"]
    out = []
    for b in ST.list_batches(pid):
        ships = b["shipments"]
        if all(s.get("cancelled") for s in ships):      # отменённая партия — показываем её состав
            ships = [dict(s, cancelled=False) for s in ships]
        out.append({"id": b["id"], "created": b["created"], "title": b.get("title"), "phase": b["phase"],
                    "cities": len([s for s in ships if not s.get("cancelled")]),
                    "boxes": sum(len(s["boxes"]) for s in ships if not s.get("cancelled")),
                    "done": len([s for s in ships if s["status"] == "done"]),
                    "slot": b.get("chosen_slot_human"), "running": W.is_running(b["id"]),
                    "demo": b.get("demo"),
                    "labels": len(b.get("groups") or []) if b.get("files") else 0,
                    "labels_done": len([g for g in b.get("groups") or []
                                        if g["id"] in ST.load_labels_done(pid, b["id"])]) if b.get("files") else 0})
    return {"batches": out}


def api_batch(b):
    pid = ST.get_profile()["id"]
    bt = ST.load_batch(pid, b["id"])
    if not bt:
        raise OzonError("Партия не найдена")
    v = _batch_view(pid, bt)
    if bt["phase"] == "await_slot":
        import slots as S
        if bt.get("multi"):
            lists = [(bt.get("order") or {}).get("slots") or []]      # одна заявка — общие слоты
        else:
            ok = [s for s in bt["shipments"] if s.get("slots") and not s.get("cancelled") and s.get("status") == "draft_ok"]
            lists = [s["slots"] for s in ok]
        v["slot_options"] = [{"slot": sl, "count": n, "human": S.human(sl)} for sl, n in S.slot_counts(lists)]
        v["slot_total"] = len(lists)
    return {"batch": v}


def api_batch_slot(b):
    import slots as S
    pid = ST.get_profile()["id"]
    bt = ST.load_batch(pid, b["id"])
    bt["chosen_slot"] = b["slot"]
    bt["chosen_slot_human"] = S.human(b["slot"])
    bt.setdefault("log", []).append({"t": time.strftime("%H:%M:%S"), "city": "",
                                     "msg": f"Выбран таймслот {S.human(b['slot'])}", "lvl": "info"})
    ST.save_batch(pid, bt)
    W.start(pid, b["id"])
    return {"ok": True}


def api_batch_resume(b):
    pid = ST.get_profile()["id"]
    bt = ST.load_batch(pid, b["id"])
    if b.get("reset_slot"):
        bt["chosen_slot"] = None
        bt["chosen_slot_human"] = None
        ST.save_batch(pid, bt)
    return {"started": W.start(pid, b["id"])}


def api_batch_stop(b):
    W.stop(b["id"])
    return {"ok": True}


def api_batch_cancel(b):
    """Запускает отмену в фоне (если поставки создаются — сначала останавливает)."""
    pid = ST.get_profile()["id"]
    W.start_cancel(pid, b["id"], b.get("cluster_ids"))
    return {"started": True}


def api_batch_replace(b):
    """Перенаправить поставку, которую Озон не принял, в другой кластер."""
    cid, name = str(b.get("cluster_id") or ""), b.get("cluster_name") or ""
    if not cid:
        raise OzonError("Выберите кластер")
    return _redo_shipment(b["id"], b["key"], cid, name, False)


def api_batch_add_supply(b):
    """Добавить в партию уже созданные в Озоне поставки по номеру заявки / поставки (несколько — через запятую)."""
    pid = ST.get_profile()["id"]
    bt = ST.load_batch(pid, b["id"])
    if not bt:
        raise OzonError("Нет такой партии")
    if bt.get("phase") in ("cancelling", "cancelled"):
        raise OzonError("Партия отменена — добавлять в неё нельзя")
    numbers = [x.strip() for x in re.split(r"[,;\n]+", str(b.get("numbers") or "")) if x.strip()]
    if not numbers:
        raise OzonError("Введите номер заявки или поставки")
    added, errors = [], []
    for num in numbers:
        try:
            ships, info = W.fetch_import(pid, b["id"], num, bt)
        except OzonError as e:
            errors.append(str(e))
            continue
        prof = ST.get_profile()["products"]
        miss = [k for k, v in info.items() if not v.get("offer_id") and not (prof.get(k) or {}).get("offer_id")]
        found = lookup_products(miss) if miss else {}
        for sku, v in info.items():
            saved = prof.get(sku) or {}
            v["offer_id"] = v.get("offer_id") or saved.get("offer_id") or (found.get(sku) or {}).get("offer_id")
            v["name"] = saved.get("name") or v.get("name") or (found.get(sku) or {}).get("name") or sku
            v["short"] = saved.get("short") or ""
            v["box_size"] = saved.get("box_size")
        if W.is_running(b["id"]):
            W.ADD.setdefault(b["id"], []).append((ships, info))
            added += [s["name"] for s in ships]
            bt["shipments"] += ships              # только для проверки дублей в этом же запросе
            continue
        bt = ST.load_batch(pid, b["id"])
        new = W.merge_import(bt, ships, info)
        added += [s["name"] for s in new]
        if bt.get("phase") in ("done", "cancelled"):
            bt["phase"] = "partial"
        ST.save_batch(pid, bt)
    if added and not W.is_running(b["id"]) and bt.get("phase") != "await_slot":
        W.start(pid, b["id"])
    if not added:
        raise OzonError("; ".join(errors) or "Ничего не добавлено")
    return {"added": added, "errors": errors}


def api_batch_retry(b):
    """Ещё раз попробовать создать заявку в тот же кластер."""
    return _redo_shipment(b["id"], b["key"], None, None, True)


def _redo_shipment(bid, key, cid, name, retry):
    pid = ST.get_profile()["id"]
    if W.is_running(bid):
        bt = ST.load_batch(pid, bid)
        if bt.get("phase") == "cancelling":
            raise OzonError("Сейчас отменяются заявки — дождитесь окончания")
        W.REPLACE.setdefault(bid, []).append((key, cid, name, retry))
        return {"queued": True}
    bt = ST.load_batch(pid, bid)
    W.apply_replacement(bt, key, cid, name, retry)
    if bt.get("phase") in ("done", "cancelled"):
        bt["phase"] = "partial"
    ST.save_batch(pid, bt)
    W.start(pid, bid)
    return {"started": True}


def api_batch_check_labels(b):
    """Пересобрать файлы этикеток и проверить: каждая этикетка ровно один раз, в своём файле."""
    pid = ST.get_profile()["id"]
    if W.is_running(b["id"]):
        raise OzonError("Дождитесь окончания создания поставок")
    r = W.Run(pid, b["id"], threading.Event())
    try:
        r.rebuild_outputs()
        chk = L.verify_outputs(r.batch, r.dir)
        r.batch["labels_check"] = chk
        r.log(("Проверка этикеток: " if chk["ok"] else "Проверка этикеток не пройдена: ") + chk["summary"],
              lvl="info" if chk["ok"] else "error")
    finally:
        r._apilog.close()
    return {"check": chk}


def api_batch_relabel(b):
    """Заново получить этикетки — по одной на каждое грузоместо (для поставок с проблемой или всех)."""
    pid = ST.get_profile()["id"]
    if W.is_running(b["id"]):
        raise OzonError("Дождитесь окончания создания поставок")
    bt = ST.load_batch(pid, b["id"])
    keys = set(b.get("keys") or [])
    n = 0
    for s in bt["shipments"]:
        if s.get("cancelled") or not s.get("supply_id") or not s.get("cargo_ids"):
            continue
        if keys and W.skey(s) not in keys:
            continue
        s["labels"], s["cargo_pdf"] = {}, {}
        s["labels_per_cargo"] = True
        s["status"] = "labels"
        n += 1
    if not n:
        raise OzonError("Нет поставок, для которых можно перезапросить этикетки")
    bt["labels_check"] = None
    bt["phase"] = "partial"
    bt.setdefault("log", []).append({"t": time.strftime("%H:%M:%S"), "city": "",
                                     "msg": f"Перезапрашиваю этикетки по одной на грузоместо: поставок {n}", "lvl": "info"})
    ST.save_batch(pid, bt)
    W.start(pid, b["id"])
    return {"started": n}


def api_batch_ack(b):
    """Пользователь увидел итог отмены — больше окно не показываем."""
    pid = ST.get_profile()["id"]
    bt = ST.load_batch(pid, b["id"])
    if bt and bt.get("cancel_result"):
        bt["cancel_result"]["shown"] = True
        ST.save_batch(pid, bt)
    return {"ok": True}


def api_batch_label_done(b):
    """Отметить (или снять отметку), что этикетки этого состава напечатаны и наклеены."""
    pid = ST.get_profile()["id"]
    if not ST.load_batch(pid, b["id"]):
        raise OzonError("Нет такой партии")
    return {"labels_done": ST.set_label_done(pid, b["id"], str(b["group"]), bool(b.get("done")))}


def _sku(v):
    sku = str(v or "").strip()
    if not re.fullmatch(r"\d{1,20}", sku):
        raise OzonError("Неверный SKU")
    return sku


def api_product_label_upload(b):
    """Загрузить этикетку товара (PDF из кабинета Озона). Хранится одна на SKU для всех партий."""
    import base64
    pid = ST.get_profile()["id"]
    sku = _sku(b.get("sku"))
    try:
        data = base64.b64decode(b.get("data") or "")
    except Exception:
        raise OzonError("Не удалось прочитать файл")
    if not data.startswith(b"%PDF"):
        raise OzonError("Это не PDF. Скачайте этикетку товара в кабинете Озона и загрузите её сюда")
    if len(data) > 10 * 1024 * 1024:
        raise OzonError("Файл больше 10 МБ")
    path = ST.product_label_path(pid, sku)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        size = L.check_label_pdf(tmp)
    except Exception as e:
        os.remove(tmp)
        raise OzonError(f"Не удалось открыть PDF: {e}")
    os.replace(tmp, path)
    return {"ok": True, "size": size}


def _barcodes_view(pid, b):
    """Для блока «Штрихкоды товаров»: по каждому SKU — сколько штук, в сколько городов, есть ли этикетка."""
    out = []
    for p in b.get("products") or []:
        plan = L.barcode_plan(b, p["sku"])
        if not plan and not L.failed_cities(b, p["sku"]):
            continue
        cargo, missing = L.cargo_plan(b, ST.batch_dir(pid, b["id"]), p["sku"]) if b.get("files") else ([], 0)
        out.append({"sku": str(p["sku"]), "name": p.get("short") or p.get("name") or str(p["sku"]),
                    "offer_id": p.get("offer_id"), "units": sum(q for _, q in plan), "cities": len(plan),
                    "has_label": os.path.isfile(ST.product_label_path(pid, p["sku"])),
                    "cargo": bool(cargo), "cargo_missing": missing,
                    "failed": L.failed_cities(b, p["sku"])})
    return out


def barcodes_file(bid, sku):
    """Собирает PDF штрихкодов товара для партии. Возвращает (путь, имя файла)."""
    pid = ST.get_profile()["id"]
    sku = _sku(sku)
    bt = ST.load_batch(pid, bid)
    if not bt:
        raise OzonError("Нет такой партии")
    label = ST.product_label_path(pid, sku)
    if not os.path.isfile(label):
        raise OzonError("Сначала загрузите этикетку товара из кабинета Озона")
    plan = L.barcode_plan(bt, sku)
    if not plan:
        raise OzonError("Этого товара нет в поставках партии")
    p = next((x for x in bt.get("products") or [] if str(x["sku"]) == sku), {})
    name = L.safe_name(f"Штрихкоды — {p.get('short') or p.get('name') or sku} — {sum(q for _, q in plan)} шт") + ".pdf"
    out = os.path.join(ST.batch_dir(pid, bid), "Штрихкоды", name)
    L.build_barcodes(label, plan, out, p.get("short") or p.get("name") or sku)
    return out, name


def api_batch_rename(b):
    pid = ST.get_profile()["id"]
    if not ST.load_batch(pid, b["id"]):
        raise OzonError("Нет такой партии")
    title = re.sub(r"\s+", " ", str(b.get("title") or "")).strip()[:120]
    ST.rename_batch(pid, b["id"], title)
    return {"title": title}


def cargo_file(bid, sku):
    """Этикетки грузомест одного товара (оригиналы Озона) по городам — как штрихкоды. Возвращает (путь, имя)."""
    pid = ST.get_profile()["id"]
    sku = _sku(sku)
    bt = ST.load_batch(pid, bid)
    if not bt:
        raise OzonError("Нет такой партии")
    bdir = ST.batch_dir(pid, bid)
    paths, _ = L.cargo_plan(bt, bdir, sku)
    if not paths:
        raise OzonError("Этикеток грузомест этого товара пока нет")
    p = next((x for x in bt.get("products") or [] if str(x["sku"]) == sku), {})
    name = L.safe_name(f"Грузоместа — {p.get('short') or p.get('name') or sku}") + ".pdf"
    out = os.path.join(bdir, "Грузоместа по товарам", name)
    L.build_cargo(paths, out, p.get("short") or p.get("name") or sku)
    return out, name


def matrix_template(b):
    """Шаблон Excel «кластеры × SKU» с текущими товарами и кластерами из формы. Возвращает (данные, имя)."""
    import xlsx as X
    saved = ST.get_profile().get("products") or {}
    prods = []
    for p in b.get("products") or []:
        sku = str(p.get("sku") or "").strip()
        if not sku:
            continue
        s = saved.get(sku, {})
        prods.append({"sku": sku, "name": p.get("short") or s.get("short") or p.get("name") or s.get("name") or "",
                      "box_size": p.get("box_size") or s.get("box_size")})
    data = X.build_template(prods, [str(c) for c in b.get("clusters") or [] if c],
                            [str(c) for c in b.get("all_clusters") or [] if c])
    return data, f"Поставки Ozon — шаблон распределения {time.strftime('%Y-%m-%d')}.xlsx"


def api_matrix_import(b):
    """Заполненная таблица Excel (.xlsx или .csv) → товары и количества по кластерам."""
    import base64
    import xlsx as X
    try:
        data = base64.b64decode(b.get("data") or "")
    except Exception:
        raise OzonError("Не удалось прочитать файл")
    if len(data) > 10 * 1024 * 1024:
        raise OzonError("Файл больше 10 МБ")
    try:
        return X.read_table(b.get("name") or "", data)
    except X.TableError as e:
        raise OzonError(str(e))
    except Exception as e:
        raise OzonError(f"Не удалось прочитать таблицу: {e}")


def api_batch_delete(b):
    """Удалить партию из истории — только если в Озоне у неё нет действующих заявок."""
    import shutil
    pid = ST.get_profile()["id"]
    bt = ST.load_batch(pid, b["id"])
    if not bt:
        raise OzonError("Партия не найдена")
    if W.is_running(b["id"]):
        raise OzonError("Партия ещё выполняется")
    live = [s["name"] for s in bt["shipments"] if s.get("order_id") and not s.get("cancelled")]
    if live:
        raise OzonError("У партии есть неотменённые заявки: " + ", ".join(live))
    path = os.path.realpath(ST.batch_dir(pid, b["id"]))
    root = os.path.realpath(os.path.join(ST.DATA, "shops", pid, "batches"))
    if os.path.dirname(path) != root:
        raise OzonError("Неверная папка партии")
    shutil.rmtree(path)
    return {"ok": True}


def api_batch_sync(b):
    pid = ST.get_profile()["id"]
    if W.is_running(b["id"]):
        raise OzonError("Дождитесь окончания создания поставок")
    bad = W.sync_batch(pid, b["id"])
    return {"mismatch": bad}


def api_batch_rebuild(b):
    pid = ST.get_profile()["id"]
    r = W.Run(pid, b["id"], threading.Event())
    try:
        r.rebuild_outputs()
    finally:
        r._apilog.close()
    return {"ok": True}


def api_open_folder(b):
    _local_only()
    pid = ST.get_profile()["id"]
    path = ST.DATA if b.get("id") == "__data__" else ST.batch_dir(pid, b["id"])
    if b.get("sub"):
        path = os.path.join(path, b["sub"])
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return {"ok": True}


ROUTES = {
    "/api/state": api_state, "/api/profile/save": api_profile_save, "/api/profile/new": api_profile_new,
    "/api/profile/select": api_profile_select, "/api/profile/delete": api_profile_delete,
    "/api/check": api_check, "/api/clusters": api_clusters, "/api/dropoff": api_dropoff,
    "/api/dropoff/resolve": api_dropoff_resolve, "/api/dropoff/fav": api_dropoff_fav,
    "/api/products": api_products, "/api/plan": api_plan, "/api/batch/create": api_batch_create, "/api/batches": api_batches,
    "/api/batch": api_batch, "/api/batch/slot": api_batch_slot, "/api/batch/resume": api_batch_resume, "/api/batch/add_supply": api_batch_add_supply,
    "/api/batch/stop": api_batch_stop, "/api/batch/cancel": api_batch_cancel,
    "/api/batch/rebuild": api_batch_rebuild,
    "/api/batch/ack": api_batch_ack, "/api/matrix/import": api_matrix_import, "/api/batch/rename": api_batch_rename, "/api/product_label/upload": api_product_label_upload, "/api/batch/label_done": api_batch_label_done, "/api/batch/check_labels": api_batch_check_labels,
    "/api/batch/relabel": api_batch_relabel, "/api/batch/replace": api_batch_replace, "/api/batch/retry": api_batch_retry, "/api/batch/delete": api_batch_delete,
    "/api/export": api_export, "/api/import": api_import,
    "/api/legacy/find": api_legacy_find, "/api/legacy/import": api_legacy_import, "/api/batch/sync": api_batch_sync, "/api/open": api_open_folder,
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _shop_from_cookie(self):
        """Выбранный в этом браузере магазин — у каждого пользователя свой."""
        pid = None
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and re.fullmatch(r"[0-9a-zA-Z_-]{1,40}", v):
                pid = v
        ST.set_current(pid)
        return pid

    def do_GET(self):
        self._shop_from_cookie()
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        if path == "/export":
            data = json.dumps(api_export({}), ensure_ascii=False, indent=1).encode("utf-8")
            name = urllib.parse.quote(f"Поставки Ozon — настройки {time.strftime('%Y-%m-%d')}.json")
            return self._send(200, data, "application/json; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename=\"ozon-settings.json\"; filename*=UTF-8''{name}"})
        if path == "/matrix-template.xlsx":
            try:
                q = urllib.parse.parse_qs(u.query).get("d", ["{}"])[0]
                data, name = matrix_template(json.loads(q))
            except Exception as e:
                return self._send(400, f"Не удалось собрать шаблон: {e}".encode("utf-8"), "text/plain; charset=utf-8")
            return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": f"attachment; filename=\"template.xlsx\"; filename*=UTF-8''{urllib.parse.quote(name)}"})
        if path.startswith(("/barcodes/", "/cargo/")):
            parts = path.split("/")
            try:
                fn = barcodes_file if parts[1] == "barcodes" else cargo_file
                full, name = fn(parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            except OzonError as e:
                return self._send(400, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            with open(full, "rb") as f:
                return self._send(200, f.read(), "application/pdf",
                                  {"Content-Disposition": f"inline; filename*=UTF-8''{urllib.parse.quote(name)}"})
        if path.startswith("/files/"):
            _, _, bid, rel = path.split("/", 3)
            base = os.path.realpath(ST.batch_dir(ST.get_profile()["id"], bid))
            full = os.path.realpath(os.path.join(base, rel))
            if not full.startswith(base + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "нет файла"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)
        if path in ("/", ""):
            path = "/index.html"
        full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(os.path.realpath(STATIC)) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        fn = ROUTES.get(urllib.parse.urlparse(self.path).path)
        if not fn:
            return self._send(404, {"error": "нет такого метода"})
        n = int(self.headers.get("Content-Length") or 0)
        before = self._shop_from_cookie()
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            res = fn(body)
            extra = None
            if ST.current() and ST.current() != before:
                extra = {"Set-Cookie": f"{COOKIE}={ST.current()}; Path=/; Max-Age=31536000; SameSite=Lax"}
            self._send(200, res, extra=extra)
        except OzonError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"Внутренняя ошибка: {e}"})


def _call(port, path, timeout=3):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def already_running(port):
    """True, если на порту уже работает эта же версия программы.

    Если там работает другая (старая) версия — просим её закрыться, чтобы запустилась новая.
    Старая версия не закроется, только если у неё прямо сейчас создаются поставки."""
    try:
        info = _call(port, "/api/ping", 2)
    except Exception:
        return False
    if info.get("app") != "ozon-supplies":
        return False
    if info.get("version") == VERSION:
        return True
    print(f"Запущена версия {info.get('version')} — закрываю её, запускаю {VERSION}")
    try:
        _call(port, "/api/quit", 5)
    except Exception:
        return True            # старая версия занята созданием поставок — открываем её
    for _ in range(40):
        time.sleep(0.25)
        try:
            _call(port, "/api/ping", 1)
        except Exception:
            return False       # порт освободился
    return True


def serve_server_mode(port):
    """Сервер (Docker): один процесс, слушает все адреса, браузер не открывает."""
    host = os.environ.get("HOST") or "0.0.0.0"
    os.makedirs(ST.DATA, exist_ok=True)
    migrate_clusters()
    _mark_interrupted()
    global SERVER
    SERVER = ThreadingHTTPServer((host, port), H)
    SERVER.daemon_threads = True
    _stop_on_sigterm()
    print(f"Поставки Ozon {VERSION} (сервер) слушают http://{host}:{port}/, данные: {ST.DATA}", flush=True)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVER.server_close()
        busy = [b for b in list(W.RUNNING) if W.is_running(b)]
        if busy:
            print(f"Жду, пока партии доделают текущий шаг: {len(busy)}…", flush=True)
            left = W.shutdown(int(os.environ.get("STOP_WAIT") or 50))
            if left:
                print(f"Не дождалась {left} — при запуске они будут отмечены «Продолжить»", flush=True)
        print("Остановлена", flush=True)


def _mark_interrupted():
    try:
        n = W.mark_interrupted()
        if n:
            print(f"Прерванных партий: {n} — у них кнопка «Продолжить»", flush=True)
    except Exception:
        traceback.print_exc()


def _stop_on_sigterm():
    """Docker останавливает контейнер сигналом SIGTERM. Python в роли главного процесса контейнера
    его не обрабатывает — Docker ждал 10 секунд и убивал программу. Теперь serve_forever прерывается,
    создаваемые партии останавливаются после текущего шага (W.shutdown), файлы пишутся атомарно."""
    import signal

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)


def main():
    port = int(os.environ.get("PORT") or 8765)
    if SERVER_MODE:
        return serve_server_mode(port)
    if getattr(sys, "frozen", False):
        # у .exe нет консоли — пишем вывод в файл рядом с данными
        os.makedirs(ST.DATA, exist_ok=True)
        log = open(os.path.join(ST.DATA, "program.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    if already_running(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    try:
        t0 = time.time()
        src = ST.migrate_legacy()
        print(f"{time.strftime('%H:%M:%S')} поиск старых настроек: {time.time() - t0:.1f} с"
              + (f", перенесены из {src}" if src else ""))
    except Exception:
        traceback.print_exc()
    migrate_clusters()
    if already_running(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    _mark_interrupted()
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            port = p
            break
        except OSError:
            continue
    if not srv:
        print("Не удалось занять порт")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print(f"Поставки Ozon {VERSION} запущены: {url}\nНе закрывайте это окно, пока работаете с программой.")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    global SERVER
    SERVER = srv
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


SERVER = None


_UPD = {"at": 0, "res": None}


def api_update(b):
    """Проверяет на GitHub, нет ли версии новее. Без интернета или к закрытому репозиторию — просто «не удалось»."""
    if _UPD["res"] and time.time() - _UPD["at"] < 600 and not b.get("force"):
        return dict(_UPD["res"], busy=any(W.is_running(x) for x in list(W.RUNNING)))
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "ozon-supplies-app"})
        with urllib.request.urlopen(req, timeout=6, context=tls.context()) as r:
            js = json.loads(r.read().decode("utf-8"))
        latest = js.get("tag_name") or js.get("name") or ""
        asset = next((a.get("browser_download_url") for a in js.get("assets") or []
                      if str(a.get("name", "")).lower().endswith((".zip", ".exe"))), None)
        res = {"ok": True, "latest": latest.lstrip("vV"), "newer": V.parse(latest) > V.parse(VERSION),
               "url": asset or js.get("html_url"), "page": js.get("html_url"), "notes": (js.get("body") or "")[:2000]}
    except urllib.error.HTTPError as e:
        res = {"ok": False, "error": "выпусков пока нет" if e.code == 404 else f"GitHub ответил {e.code}"}
    except Exception as e:
        res = {"ok": False, "error": "нет связи с GitHub"}
    _UPD.update(at=time.time(), res=res)
    return dict(res, busy=any(W.is_running(x) for x in list(W.RUNNING)))


def api_ping(_):
    return {"app": "ozon-supplies", "version": VERSION, "tls": type(tls.context()).__module__.split(".")[0]}


def api_quit(_):
    _local_only()
    if any(W.is_running(b) for b in list(W.RUNNING)):
        raise OzonError("Сейчас создаются поставки — дождитесь окончания или остановите их")
    threading.Timer(0.3, lambda: SERVER and SERVER.shutdown()).start()
    return {"ok": True}


ROUTES["/api/ping"] = api_ping
ROUTES["/api/update"] = api_update
ROUTES["/api/quit"] = api_quit


if __name__ == "__main__":
    main()
