"""Демо-Озон: имитирует нужные методы API, чтобы попробовать программу без магазина.

Ничего не отправляет в настоящий Озон. Включается в настройках магазина («Демо-режим»).
"""
import os
import random
import threading
import time
from datetime import date, timedelta

SPEED = 0.02        # в демо паузы между запросами в 50 раз короче
_instances = {}
_lock = threading.Lock()

CLUSTERS = [
    (4039, "Москва, МО и Дальние регионы"), (4002, "Санкт-Петербург и СЗО"), (4007, "Ростов"),
    (4071, "Тверь"), (4038, "Ярославль"), (4040, "Екатеринбург"), (4074, "Пермь"), (4045, "Оренбург"),
    (4042, "Казань"), (4044, "Воронеж"), (4026, "Краснодар"), (4073, "Уфа"), (4072, "Саратов"),
    (4075, "Невинномысск"), (4050, "Новосибирск"), (4053, "Самара"), (4032, "Красноярск"),
    (4060, "Калининград"), (4049, "Омск"), (4065, "Дальний Восток"),
]
DROPOFFS = [
    (1020000081101000, "Москва_Хоругвино_Кросс-докинг", "WAREHOUSE_TYPE_CROSS_DOCK", "МО, Солнечногорск, Хоругвино"),
    (1020001426271000, "Москва_Ногинск_ППЗ", "WAREHOUSE_TYPE_ORDERS_RECEIVING_POINT", "МО, Ногинск"),
    (1020000037452000, "Санкт-Петербург_Шушары_Кросс-докинг", "WAREHOUSE_TYPE_CROSS_DOCK", "СПб, Шушары"),
    (1020000110022000, "Москва_Пушкино_ПВЗ", "WAREHOUSE_TYPE_DELIVERY_POINT", "МО, Пушкино, ул. Тихая 5"),
    (1020001505318000, "СЦ Кавказский", "WAREHOUSE_TYPE_SORTING_CENTER", "Москва, Кавказский бульвар, 57"),
    (1020001623486000, "СЦ Строгино", "WAREHOUSE_TYPE_SORTING_CENTER", "Москва, Строгинский бульвар, 14"),
]


def get(pid):
    with _lock:
        if pid not in _instances:
            _instances[pid] = MockOzon()
        return _instances[pid]


def _pdf(lines_per_page):
    """Минимальный PDF: по странице на этикетку (ASCII-текст)."""
    objs = []
    n = len(lines_per_page)
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(n))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    font_id = 3 + n * 2
    for i, lines in enumerate(lines_per_page):
        content_id = 4 + i * 2
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 340 227] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        text = "BT /F1 14 Tf 20 190 Td 18 TL " + " ".join(
            "(" + l.replace("\\", "").replace("(", "").replace(")", "") + ") '" for l in lines) + " ET"
        objs.append(f"<< /Length {len(text)} >>\nstream\n{text}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


class MockOzon:
    def __init__(self):
        self.seq = 1000
        self.drafts = {}
        self.orders = {}
        self.supplies = {}
        self.ops = {}
        self.files = {}
        self.calls = []
        self.lock = threading.Lock()
        # Фиксированный набор слотов на ближайшие 14 дней (у всех кластеров одинаковый — как у кросс-докинга)
        self.base_slots = []
        d0 = date.today()
        for d in range(1, 15):
            day = d0 + timedelta(days=d)
            if day.weekday() == 6:
                continue
            for h in (9, 12, 15):
                self.base_slots.append((f"{day}T{h:02d}:00:00Z", f"{day}T{h + 3:02d}:00:00Z"))

    def _id(self):
        self.seq += 1
        return self.seq

    def handle(self, path, body):
        with self.lock:
            self.calls.append(path)
            # как у настоящего Озона: не больше ~5 запросов в секунду (в демо секунда сжата в SPEED раз)
            now = time.time()
            self.recent = [t for t in getattr(self, "recent", []) if now - t < 1.0 * SPEED] + [now]
            if len(self.recent) > 5:
                self.rate_limited = getattr(self, "rate_limited", 0) + 1
                return 429, {"code": 8, "message": "You have reached request rate limit per second"}
            fn = getattr(self, "m_" + path.strip("/").replace("/", "_").replace("-", "_"), None)
            if not fn:
                return 404, {"code": 5, "message": f"демо: метод {path} не реализован"}
            try:
                return 200, fn(body or {})
            except KeyError as e:
                return 400, {"code": 3, "message": f"демо: не найдено {e}"}

    def get_file(self, url):
        guid = url.rsplit("/", 1)[-1]
        return self.files[guid]

    # ---- справочники
    def m_v1_roles(self, b):
        return {"roles": [{"name": "Supply", "methods": ["/v1/draft/crossdock/create"]}]}

    def m_v2_cluster_list(self, b):
        return {"result": [{"macrolocal_cluster_id": cid, "data": {"macrolocal_cluster": {"name": n}}}
                           for cid, n in CLUSTERS]}

    def m_v1_cluster_list(self, b):
        # как у настоящего Озона: id — свой справочник, macrolocal_cluster_id — внутри logistic_clusters
        return {"clusters": [{"id": 100 + i, "name": n, "type": "CLUSTER_TYPE_OZON",
                              "logistic_clusters": [{"macrolocal_cluster_id": cid, "warehouses": []}]}
                             for i, (cid, n) in enumerate(CLUSTERS)]}

    def m_v1_warehouse_fbo_list(self, b):
        q = (b.get("search") or "").lower()
        return {"search": [{"warehouse_id": i, "name": n, "warehouse_type": t, "address": a}
                           for i, n, t, a in DROPOFFS if q in n.lower() or q in a.lower()]}

    def m_v3_product_info_list(self, b):
        items = []
        for sku in b.get("sku") or []:
            s = str(sku)
            items.append({"id": int(s) + 5, "sku": int(s), "offer_id": "ART-" + s[-4:],
                          "name": DEMO_NAMES.get(s, f"Демо-товар {s}"), "barcodes": ["46" + s[-11:]]})
        return {"items": items}

    # ---- черновики
    def _new_draft(self, clusters_info):
        known = dict(CLUSTERS)
        refuse = {x.strip() for x in os.environ.get("MOCK_REFUSE", "").split(",") if x.strip()}
        bad = [str(c["macrolocal_cluster_id"]) for c in clusters_info
               if int(c["macrolocal_cluster_id"]) not in known or str(c["macrolocal_cluster_id"]) in refuse]
        if bad:
            return {"draft_id": 0, "errors": [{"error_message": "CAN_NOT_CREATE_DRAFT",
                                               "error_reasons": ["NOT_AVAILABLE_CLUSTERS"],
                                               "macrolocal_cluster_ids": bad}]}
        did = self._id()
        self.drafts[did] = {"t": time.time(), "polls": 0, "clusters": [
            {"cluster": int(c["macrolocal_cluster_id"]), "items": c["items"],
             "rejected": [i["sku"] for i in c["items"] if str(i["sku"]).endswith("999")]} for c in clusters_info]}
        return {"draft_id": did, "errors": []}

    def m_v1_draft_crossdock_create(self, b):
        return self._new_draft([b["cluster_info"]])

    def m_v1_draft_multi_cluster_create(self, b):
        return self._new_draft(b["clusters_info"])

    def m_v2_draft_create_info(self, b):
        d = self.drafts[b["draft_id"]]
        d["polls"] += 1
        if d["polls"] < 2:
            return {"status": "IN_PROGRESS"}
        errors = []
        for c in d["clusters"]:
            if c["rejected"]:
                errors.append({"error_message": "", "error_reasons": [], "items_validation": [{
                    "macrolocal_cluster_id": c["cluster"],
                    "rejected_items": [{"sku": s, "reasons": ["SKU_IS_NOT_ACCEPTED_IN_CLUSTER"]} for s in c["rejected"]]}]})
        return {"status": "SUCCESS", "errors": errors, "clusters": [{
            "cluster_name": dict(CLUSTERS).get(c["cluster"], "?"), "macrolocal_cluster_id": c["cluster"],
            "supply_type": "CROSSDOCK", "warehouses": [{
                "availability_status": {"state": "FULL_AVAILABLE", "invalid_reason": "UNSPECIFIED"},
                "storage_warehouse": {"warehouse_id": c["cluster"] * 10, "name": f"Склад {c['cluster']}"},
                "total_rank": 1, "total_score": 9.5}]} for c in d["clusters"]]}

    def m_v2_draft_timeslot_info(self, b):
        d = self.drafts[b["draft_id"]]
        sel = sorted(int(x["macrolocal_cluster_id"]) for x in b["selected_cluster_warehouses"])
        if sel != sorted(c["cluster"] for c in d["clusters"]):
            return {"error_reason": "INVALID_CLUSTERS_COUNT"}
        if len(d["clusters"]) > 1:
            # общая точка: слотов меньше и они позже
            slots = [s for s in self.base_slots[9:] if s[0][11:13] == "12"]
        else:
            rnd = random.Random(d["clusters"][0]["cluster"])
            slots = [s for s in self.base_slots if rnd.random() > 0.15]   # у кластеров часть слотов пропадает
        days = {}
        for f, t in slots:
            days.setdefault(f[:10], []).append({"from_in_timezone": f, "to_in_timezone": t})
        return {"error_reason": "UNSPECIFIED", "result": {"drop_off_warehouse_timeslots": {
            "current_time_in_timezone": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "warehouse_timezone": "Europe/Moscow",
            "days": [{"date_in_timezone": k, "timeslots": v} for k, v in sorted(days.items())]},
            "requested_date_from": b["date_from"], "requested_date_to": b["date_to"]}}

    def m_v2_draft_supply_create(self, b):
        d = self.drafts[b["draft_id"]]
        if time.time() - d["t"] > 30 * 60:
            return {"error_reasons": ["DRAFT_DOES_NOT_EXIST"]}
        if len(b["selected_cluster_warehouses"]) != len(d["clusters"]):
            return {"error_reasons": ["INVALID_CLUSTERS_COUNT"]}
        # каждый третий черновик: выбранный слот «успели занять» с первой попытки
        if b["draft_id"] % 3 == 0 and not d.get("slot_taken_once"):
            d["slot_taken_once"] = True
            return {"draft_id": b["draft_id"], "error_reasons": ["TIMESLOT_NOT_AVAILABLE"]}
        oid = self._id()
        sids = []
        for c in d["clusters"]:
            sid = 2000000000 + self._id()
            self.supplies[sid] = {"items": [i for i in c["items"] if i["sku"] not in c["rejected"]],
                                  "cargoes": {}, "cluster": c["cluster"]}
            sids.append(sid)
        self.orders[oid] = {"order_id": oid, "order_number": f"{oid}-DEMO", "state": "DATA_FILLING",
                            "timeslot": b.get("timeslot"), "supplies": sids, "polls": 0}
        d["order"] = oid
        return {"draft_id": b["draft_id"], "error_reasons": []}

    def m_v2_draft_supply_create_status(self, b):
        d = self.drafts[b["draft_id"]]
        return {"status": "SUCCESS", "order_id": d["order"], "error_reasons": []}

    def m_v3_supply_order_get(self, b):
        out = []
        for oid in b["order_ids"]:
            o = self.orders[int(oid)]
            o["polls"] += 1
            sup = [{"supply_id": s, "state": "DATA_FILLING", "macrolocal_cluster_id": self.supplies[s]["cluster"],
                    "storage_warehouse": {"name": "Демо-склад"}}
                   for s in o["supplies"]] if o["polls"] > 1 else []
            out.append({"order_id": oid, "order_number": o["order_number"], "state": o["state"], "supplies": sup})
        return {"orders": out}

    # ---- грузоместа
    def m_v1_cargoes_create(self, b):
        sup = self.supplies[int(b["supply_id"])]
        if len(b["cargoes"]) > 30:
            return {"errors": {"error_reasons": ["CARGOES_LIMIT_EXCEEDED"]}}
        if b.get("delete_current_version"):
            sup["cargoes"] = {}
        res = []
        for c in b["cargoes"]:
            cid = self._id() + 50000000
            sup["cargoes"][cid] = c
            res.append({"key": c["key"], "value": {"cargo_id": cid}})
        op = f"op-{self._id()}"
        self.ops[op] = {"status": "SUCCESS", "result": {"cargoes": res}, "polls": 0}
        return {"operation_id": op, "errors": {}}

    def m_v2_cargoes_create_info(self, b):
        op = self.ops[b["operation_id"]]
        op["polls"] += 1
        if op["polls"] < 2:
            return {"status": "IN_PROGRESS"}
        return {"status": op["status"], "result": op["result"]}

    def m_v2_cargoes_get(self, b):
        out = []
        for req in b.get("supplies") or []:
            sup = self.supplies[int(req["supply_id"])]
            ids = {int(x) for x in req.get("cargo_ids") or []} or set(sup["cargoes"])
            out.append({"supply_id": int(req["supply_id"]), "cargoes": [
                {"cargo_id": cid, "bundle_id": f"bnd-{cid}", "type": "BOX", "content_type": "MONO"}
                for cid in sup["cargoes"] if cid in ids]})
        return {"supplies": out}

    def m_v1_supply_order_bundle(self, b):
        items = []
        for bid in b.get("bundle_ids") or []:
            cid = int(str(bid).split("-")[1])
            for sid, sup in self.supplies.items():
                if cid in sup["cargoes"]:
                    it = sup["cargoes"][cid]["value"]["items"][0]
                    sku = next((i["sku"] for i in sup["items"] if "ART-" + str(i["sku"])[-4:] == it["offer_id"]), 0)
                    qty = it["quantity"]
                    if os.environ.get("MOCK_BAD_CARGO") and cid % 7 == 0:
                        qty += 1                     # «Озон записал не то количество» — для проверки защиты
                    items.append({"sku": sku, "offer_id": it["offer_id"], "quantity": qty, "name": "демо"})
        return {"items": items, "total_count": len(items), "has_next": False}

    def m_v1_cargoes_label_create(self, b):
        sid = int(b["supply_id"])
        sup = self.supplies[sid]
        pages = []
        for c in b.get("cargoes") or []:
            cg = sup["cargoes"][c["cargo_id"]]
            it = cg["value"]["items"][0]
            first = f"OZON  CARGO {c['cargo_id']}" if not os.environ.get("MOCK_LABEL_NO_ID") else "OZON  CARGO (barcode)"
            offer = it.get("offer_id")
            if os.environ.get("MOCK_ALIEN_LABEL") and c["cargo_id"] % 5 == 0:
                offer = "ART-0064" if offer != "ART-0064" else "ART-0032"     # «Озон напечатал чужой товар»
            pages.append([first, f"Supply {sid}", f"{offer} x {it['quantity']}", "DEMO LABEL"])
        guid = f"g{self._id()}"
        self.files[guid] = _pdf(pages)
        op = f"lop-{self._id()}"
        self.ops[op] = {"status": "SUCCESS", "result": {"file_guid": guid, "file_url": f"demo://file/{guid}"}, "polls": 0}
        return {"operation_id": op}

    def m_v1_cargoes_label_get(self, b):
        op = self.ops[b["operation_id"]]
        op["polls"] += 1
        if op["polls"] < 4:            # этикетки Озон готовит дольше остального
            return {"status": "IN_PROGRESS"}
        return {"status": "SUCCESS", "result": op["result"]}

    # ---- пропуск и отмена
    def m_v1_supply_order_pass_create(self, b):
        op = f"pop-{self._id()}"
        return {"operation_id": op, "error_reasons": []}

    def m_v1_supply_order_pass_status(self, b):
        return {"result": "SUCCESS", "errors": []}

    def m_v1_supply_order_cancel(self, b):
        oid = int(b["order_id"])
        o = self.orders[oid]
        o["cancel_tries"] = o.get("cancel_tries", 0) + 1
        op = f"cop-{oid}-{o['cancel_tries']}"
        # как у настоящего Озона: часть заявок с первого раза «занята другой операцией»
        busy = oid % 2 == 1 and o["cancel_tries"] == 1
        self.ops[op] = {"oid": oid, "busy": busy, "polls": 0}
        return {"operation_id": op}

    def m_v1_supply_order_cancel_status(self, b):
        op = self.ops[b["operation_id"]]
        op["polls"] += 1
        if op["polls"] < 2:
            return {"status": "IN_PROGRESS"}
        if op["busy"]:
            return {"status": "ERROR", "error_reasons": ["OTHER_ASYNCHRONOUS_OPERATION_IN_PROGRESS"]}
        self.orders[op["oid"]]["state"] = "CANCELLED"
        return {"status": "SUCCESS", "result": {"is_order_cancelled": True}}


DEMO_NAMES = {
    "1000000032": "Карта памяти microSD 32 ГБ", "1000000064": "Карта памяти microSD 64 ГБ",
    "1000000128": "Карта памяти microSD 128 ГБ", "1000000256": "Карта памяти microSD 256 ГБ",
    "1000000512": "Карта памяти microSD 512 ГБ", "1000001024": "Карта памяти microSD 1 ТБ",
    "1000002048": "Карта памяти microSD 2 ТБ", "2000000024": "Камера видеонаблюдения Wi‑Fi",
}
