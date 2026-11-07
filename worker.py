"""Создание поставок в Озоне.

Два режима:
  • «отдельно» — на каждый город своя заявка (черновик кросс-докинга на один кластер);
  • «одной заявкой» — одна заявка на все города (черновик на несколько кластеров):
    один таймслот, один пропуск, один номер заявки, внутри — поставка на каждый кластер.

Порядок: черновики → таймслот → заявки → грузоместа → этикетки → пропуск.
Каждая партия выполняется в отдельном потоке, состояние сохраняется после каждого шага,
поэтому после ошибки или перезапуска можно нажать «Продолжить».
"""
import os
import re
import threading
import time
import traceback
from datetime import date, timedelta

import labels as L
import planner as P
import slots as S
import storage as ST
from ozon_api import OzonClient, OzonError

DRAFT_TTL = 27 * 60          # черновик живёт 30 минут — пересоздаём заранее
RUNNING = {}                 # batch_id -> (thread, stop_event)
_save_lock = threading.Lock()

REASONS = {
    "NOT_AVAILABLE_CLUSTERS": "Озон не принимает поставки в этот кластер через выбранную точку отгрузки",
    "CAN_NOT_CREATE_DRAFT": "Озон не смог создать черновик",
    "TIMESLOT_NOT_AVAILABLE": "таймслот уже занят",
    "DRAFT_DOES_NOT_EXIST": "черновик устарел",
}


def dropoff_type(t):
    t = (t or "DELIVERY_POINT").upper()
    return t[len("WAREHOUSE_TYPE_"):] if t.startswith("WAREHOUSE_TYPE_") else t


def make_client(profile, log=None):
    demo = None
    speed = 1.0
    if profile.get("demo"):
        import mock_ozon
        demo = mock_ozon.get(profile["id"])
        speed = mock_ozon.SPEED
    return OzonClient(profile.get("client_id"), profile.get("api_key"), log=log, demo=demo, speed=speed)


def norm(name):
    return re.sub(r"[^0-9a-zа-я]+", "", str(name or "").lower().replace("ё", "е"))


def same_cluster(expected, got):
    a, b = norm(expected), norm(got)
    if not a or not b:
        return True          # сравнить не с чем
    return a == b or a in b or b in a


class Run:
    def __init__(self, pid, bid, stop):
        self.pid = pid
        self.bid = bid
        self.stop = stop
        self.profile = ST.get_profile(pid)
        self.batch = ST.load_batch(pid, bid)
        self.dir = ST.batch_dir(pid, bid)
        self._apilog = open(os.path.join(self.dir, "api.log"), "a", encoding="utf-8")
        self.api = make_client(self.profile, log=self._api_log)
        self.api.stop_flag = stop
        self.failed_now = set()   # города, упавшие в этом запуске, — повторно не трогаем

    # --------------------------------------------------------- служебное
    def _api_log(self, s):
        self._apilog.write(time.strftime("%H:%M:%S ") + s + "\n")
        self._apilog.flush()

    def log(self, msg, ship=None, lvl="info"):
        self.batch.setdefault("log", []).append(
            {"t": time.strftime("%H:%M:%S"), "city": ship["name"] if ship else "", "msg": msg, "lvl": lvl})
        self.batch["log"] = self.batch["log"][-800:]
        self.save()

    def save(self):
        with _save_lock:
            ST.save_batch(self.pid, self.batch)

    def set(self, ship, status, error=None):
        ship["status"] = status
        ship["error"] = error
        self.save()

    def check_stop(self):
        if self.stop.is_set():
            raise OzonError("Остановлено пользователем")

    def active(self):
        return [s for s in self.batch["shipments"] if not s.get("cancelled")]

    def delivery_info(self):
        d = self.batch["dropoff"]
        return {"type": "DROPOFF",
                "drop_off_warehouse": {"warehouse_id": int(d["warehouse_id"]),
                                       "warehouse_type": dropoff_type(d.get("warehouse_type"))}}

    @staticmethod
    def items_of(s):
        return [{"sku": int(sku), "quantity": int(q)} for sku, q in s["items"].items() if int(q) > 0]

    def check_cluster(self, s, info_cluster):
        """Защита: Озон должен создать черновик именно для того кластера, который выбран для города."""
        if not info_cluster:
            return
        got_id = str(info_cluster.get("macrolocal_cluster_id") or "")
        got_name = info_cluster.get("cluster_name") or ""
        s["ozon_cluster_name"] = got_name
        if got_id and got_id != str(s["cluster_id"]):
            raise OzonError(f"Озон вернул другой кластер ({got_name or got_id}) — поставка не создаётся")
        if not same_cluster(s.get("cluster_name"), got_name):
            raise OzonError(f"Кластер не совпадает: для города выбран «{s.get('cluster_name')}», "
                            f"а у Озона под этим ID «{got_name}». Поставка не создаётся — "
                            f"проверьте кластер города и создайте партию заново")

    def apply_rejected(self, s, rejected):
        names = {p["sku"]: (p.get("short") or p.get("name") or p["sku"]) for p in self.batch["products"]}
        for sku, reasons in rejected.items():
            if sku in s["items"]:
                s["items"].pop(sku, None)
                s["boxes"] = [b for b in s["boxes"] if b["sku"] != sku]
                msg = f"Озон не принял {names.get(sku, sku)} (SKU {sku}): {', '.join(reasons) or 'без причины'} — убрано из поставки"
                s.setdefault("warn", []).append(msg)
                self.log(msg, s, "warn")
        if not s["items"]:
            raise OzonError("Озон не принял ни одного товара в этот кластер")

    # ================================================================ ОТДЕЛЬНЫЕ ЗАЯВКИ
    def create_draft(self, s):
        self.check_stop()
        self.set(s, "draft")
        body = {"cluster_info": {"macrolocal_cluster_id": int(s["cluster_id"]), "items": self.items_of(s)},
                "deletion_sku_mode": "FULL", "delivery_info": self.delivery_info()}
        r = self.api.post("/v1/draft/crossdock/create", body)
        draft_id = r.get("draft_id")
        if not draft_id:
            raise OzonError("Черновик не создан: " + (_errors_text(r.get("errors")) or "нет draft_id"))
        info = self.poll_draft(draft_id)
        if info.get("status") != "SUCCESS":
            raise OzonError("Черновик не создан: " + (_errors_text(info.get("errors")) or info.get("status", "")))
        cl = (info.get("clusters") or [None])[0]
        self.check_cluster(s, cl)
        rejected = _rejected(r.get("errors"))
        rejected.update(_rejected(info.get("errors")))
        s["draft_id"] = draft_id
        s["draft_at"] = time.time()
        s["storage_wh"] = _best_warehouse(cl)
        if rejected:
            self.apply_rejected(s, rejected)
        s["slots"] = self.fetch_slots(draft_id, "CROSSDOCK", [s])
        s["slots_at"] = time.time()
        self.set(s, "draft_ok")
        self.log(f"Черновик {draft_id} — кластер «{s.get('ozon_cluster_name') or s['cluster_id']}», "
                 f"{len(s['slots'])} таймслотов", s)

    def poll_draft(self, draft_id):
        return self.api.poll("/v2/draft/create/info", {"draft_id": draft_id},
                             lambda x: x.get("status") not in ("IN_PROGRESS", None, "", "UNSPECIFIED"),
                             interval=3, timeout=240)

    def cluster_wh(self, ships):
        out = []
        for s in ships:
            w = {"macrolocal_cluster_id": int(s["cluster_id"])}
            if self.batch.get("use_storage") and s.get("storage_wh"):
                w["storage_warehouse_id"] = int(s["storage_wh"]["id"])
            out.append(w)
        return out

    def fetch_slots(self, draft_id, supply_type, ships):
        today = date.today()
        body = {"draft_id": draft_id, "date_from": today.isoformat(),
                "date_to": (today + timedelta(days=27)).isoformat(),
                "supply_type": supply_type, "selected_cluster_warehouses": self.cluster_wh(ships)}
        try:
            r = self.api.post("/v2/draft/timeslot/info", body)
            err = r.get("error_reason")
            if err and err not in ("UNSPECIFIED",):
                raise OzonError(f"таймслоты: {err}")
        except OzonError:
            if self.batch.get("use_storage") or not all(s.get("storage_wh") for s in ships):
                raise
            self.batch["use_storage"] = True     # второй вариант — с явным складом размещения
            body["selected_cluster_warehouses"] = self.cluster_wh(ships)
            r = self.api.post("/v2/draft/timeslot/info", body)
        t = (r.get("result") or {}).get("drop_off_warehouse_timeslots") or {}
        out = []
        for d in t.get("days") or []:
            for x in d.get("timeslots") or []:
                if x.get("from_in_timezone") and x.get("to_in_timezone"):
                    out.append([x["from_in_timezone"], x["to_in_timezone"]])
        now = t.get("current_time_in_timezone")
        if now:
            try:
                cutoff = S.parse(now) + timedelta(minutes=30)
                out = [x for x in out if S.parse(x[0]) > cutoff]
            except ValueError:
                pass
        return sorted(out, key=lambda x: S.parse(x[0]))

    def draft_alive(self, holder):
        return holder.get("draft_id") and time.time() - holder.get("draft_at", 0) < DRAFT_TTL

    def take_slot(self, available, who=None):
        """Берёт выбранный таймслот, а если его нет — ближайший (лучше позже, чем раньше).
        Взятый слот становится общим для всех следующих поставок партии."""
        want = self.batch["chosen_slot"]
        keys = {S.key(x) for x in available}
        if S.key(want) in keys:
            return want
        slot = S.nearest_to(available, want)
        if not slot:
            raise OzonError("Нет доступных таймслотов")
        msg = f"Таймслот {S.human(want)} недоступен — взят ближайший {S.human(slot)}"
        if who:
            who.setdefault("warn", []).append(msg)
        self.log(msg, who if who and "name" in who else None, "warn")
        return slot

    def auto_slot(self, s):
        """Автоматический режим, отдельные заявки: каждому городу — свой лучший слот в диапазоне."""
        pref = self.batch["slot_pref"]
        lists = [x.get("slots") or [] for x in self.active() if x.get("slots")]
        cov = {S.key(sl): n for sl, n in S.slot_counts(lists)}
        slot = S.choose_for_city(s["slots"], pref, cov)
        if not slot:
            raise OzonError("Нет доступных таймслотов")
        if S.distance(slot, pref) > 0:
            msg = f"В диапазоне свободных слотов нет — взят ближайший {S.human(slot)}"
            s.setdefault("warn", []).append(msg)
            self.log(msg, s, "warn")
        return slot

    def create_supply(self, s):
        self.check_stop()
        if not self.draft_alive(s):
            self.log("Черновик устарел — создаю заново", s)
            self.create_draft(s)
        elif time.time() - s.get("slots_at", 0) > 90:
            s["slots"] = self.fetch_slots(s["draft_id"], "CROSSDOCK", [s])
            s["slots_at"] = time.time()
        auto = self.batch.get("mode") == "auto"
        slot = self.auto_slot(s) if auto else self.take_slot(s["slots"], s)
        self.set(s, "supply")
        order_id = self.submit_supply(s["draft_id"], "CROSSDOCK", [s], slot)
        s["order_id"] = order_id
        s["slot"] = slot
        if auto:
            self.batch["chosen_slot_human"] = S.summary([x.get("slot") for x in self.active()])
        self.save()
        self.log(f"Заявка создана, id {order_id}, таймслот {S.human(slot)}", s)

    def submit_supply(self, draft_id, supply_type, ships, slot):
        body = {"draft_id": draft_id, "selected_cluster_warehouses": self.cluster_wh(ships),
                "supply_type": supply_type,
                "timeslot": {"from_in_timezone": slot[0], "to_in_timezone": slot[1]}}
        r = self.api.post("/v2/draft/supply/create", body)
        errs = [e for e in (r.get("error_reasons") or []) if e not in ("UNSPECIFIED",)]
        if errs:
            raise OzonError("Заявка не создана: " + _reasons(errs))
        st = self.api.poll("/v2/draft/supply/create/status", {"draft_id": draft_id},
                           lambda x: x.get("status") not in ("IN_PROGRESS", None, "", "UNSPECIFIED"),
                           interval=3, timeout=300)
        if st.get("status") != "SUCCESS" or not st.get("order_id"):
            raise OzonError("Заявка не создана: " + _reasons(
                [e for e in st.get("error_reasons") or [] if e != "UNSPECIFIED"] or [st.get("status", "")]))
        return st["order_id"]

    def fetch_order(self, order_id, ships):
        """Ждёт, пока Озон выдаст номера поставок, и раскладывает их по городам (по кластеру)."""
        deadline = time.time() + 240
        need = {str(s["cluster_id"]): s for s in ships if not s.get("supply_id")}
        while True:
            self.check_stop()
            r = self.api.post("/v3/supply-order/get", {"order_ids": [int(order_id)]})
            o = (r.get("orders") or [None])[0]
            if o:
                sups = o.get("supplies") or []
                for s in ships:
                    s["order_number"] = o.get("order_number") or s.get("order_number")
                    s["order_state"] = o.get("state")
                for sup in sups:
                    if not sup.get("supply_id"):
                        continue
                    ml = str(sup.get("macrolocal_cluster_id") or "")
                    s = need.get(ml)
                    if s is None and len(ships) == 1 and len(sups) == 1:
                        s = ships[0]          # одна поставка в заявке — без сверки по кластеру
                        if ml and ml != str(s["cluster_id"]):
                            raise OzonError(f"Озон создал поставку в другой кластер (ID {ml}) — "
                                            f"отмените эту заявку в кабинете")
                    if s is not None and not s.get("supply_id"):
                        s["supply_id"] = sup["supply_id"]
                        s["storage_name"] = (sup.get("storage_warehouse") or {}).get("name")
                        need.pop(str(s["cluster_id"]), None)
                self.save()
                if not need:
                    return
            if time.time() > deadline:
                raise OzonError("Озон ещё не выдал номера поставок — нажмите «Продолжить» через минуту")
            self.api.sleep(4 * self.api.speed)

    # --------------------------------------------------------- грузоместа и этикетки (параллельно)
    def cargo_phase(self, ships):
        """Грузоместа и этикетки сразу для всех поставок.

        Раньше программа ждала каждую поставку по очереди (Озон готовит этикетки 2–3 минуты),
        и 14 городов занимали почти час. Теперь запросы уходят по всем поставкам сразу,
        а ответы собираются одним общим циклом — общее время ≈ времени одной поставки.
        """
        ships = [s for s in ships if s.get("supply_id") and s["status"] != "done"
                 and s["cluster_id"] not in self.failed_now]
        if not ships:
            return
        self.cargo_all(ships)
        ok = [s for s in ships if s["cluster_id"] not in self.failed_now]
        self.labels_all(ok)
        for s in ok:
            if s["cluster_id"] in self.failed_now:
                continue
            if not self.batch.get("multi"):
                self.set(s, "pass")
                self.make_pass(s["order_id"], s, s)
            self.set(s, "done")

    def ship_error(self, s, e):
        if self.stop.is_set():
            raise OzonError("Остановлено пользователем")
        self.failed_now.add(s["cluster_id"])
        self.set(s, "error", str(e))
        self.log(str(e), s, "error")

    def cargo_all(self, ships):
        prod = {p["sku"]: p for p in self.batch["products"]}
        plan = {}
        for s in ships:
            s.setdefault("cargo_ids", {})
            todo = [b for b in s["boxes"] if str(b["key"]) not in s["cargo_ids"]]
            if todo:
                plan[s["cluster_id"]] = {"s": s, "chunks": P.chunks(todo), "first": not s["cargo_ids"]}
                self.set(s, "cargo")
        rnd = 0
        while plan:
            self.check_stop()
            ops = {}
            for cid, p in list(plan.items()):
                s = p["s"]
                part = p["chunks"].pop(0)
                try:
                    cargoes = []
                    for b in part:
                        offer = prod.get(b["sku"], {}).get("offer_id")
                        if not offer:
                            raise OzonError(f"У SKU {b['sku']} нет артикула (offer_id) — обновите данные товара в настройках")
                        cargoes.append({"key": b["key"], "value": {"type": "BOX", "items": [
                            {"offer_id": offer, "quantity": int(b["qty"])}]}})
                    r = self.api.post("/v1/cargoes/create", {
                        "supply_id": int(s["supply_id"]), "delete_current_version": bool(p["first"] and rnd == 0),
                        "cargoes": cargoes})
                    e = r.get("errors") or {}
                    if e.get("error_reasons") or e.get("items_validation"):
                        raise OzonError(_cargo_errors(e))
                    ops[cid] = r.get("operation_id")
                except OzonError as e:
                    plan.pop(cid)
                    self.ship_error(s, e)
            t0 = time.time()
            while ops:
                self.check_stop()
                for cid, op in list(ops.items()):
                    s = plan[cid]["s"]
                    try:
                        info = self.api.post("/v2/cargoes/create/info", {"operation_id": op})
                        st = info.get("status")
                        if st in ("IN_PROGRESS", None, "", "STATUS_UNSPECIFIED"):
                            continue
                        ops.pop(cid)
                        if st != "SUCCESS":
                            raise OzonError(_cargo_errors(info.get("errors") or {}) or f"Грузоместа: {st}")
                        for c in (info.get("result") or {}).get("cargoes") or []:
                            cargo_id = (c.get("value") or {}).get("cargo_id")
                            if c.get("key") and cargo_id:
                                s["cargo_ids"][str(c["key"])] = cargo_id
                        self.save()
                        if not plan[cid]["chunks"]:
                            plan.pop(cid)
                            missing = [b for b in s["boxes"] if str(b["key"]) not in s["cargo_ids"]]
                            if missing:
                                raise OzonError(f"Озон не вернул номера для {len(missing)} грузомест")
                            self.log(f"Грузоместа созданы: {len(s['boxes'])}", s)
                    except OzonError as e:
                        ops.pop(cid, None)
                        plan.pop(cid, None)
                        self.ship_error(s, e)
                if ops:
                    if time.time() - t0 > 600:
                        for cid in list(ops):
                            self.ship_error(plan.pop(cid)["s"], OzonError(
                                "Озон долго создаёт грузоместа — нажмите «Продолжить» позже"))
                        break
                    self.api.sleep(3 * self.api.speed)
            rnd += 1

    def labels_all(self, ships):
        """Этикетки: по одному запросу на каждый состав коробки в каждой поставке, все сразу."""
        os.makedirs(os.path.join(self.dir, "raw"), exist_ok=True)
        ops = []           # (поставка, состав, operation_id)
        for s in ships:
            self.check_stop()
            s.setdefault("labels", {})
            by_group = {}
            for b in s["boxes"]:
                by_group.setdefault(f"{b['sku']}x{b['qty']}", []).append(s["cargo_ids"][str(b["key"])])
            todo = {g: ids for g, ids in by_group.items()
                    if not (g in s["labels"] and os.path.exists(os.path.join(self.dir, s["labels"][g])))}
            if not todo:
                continue
            self.set(s, "labels")
            try:
                for gid, ids in todo.items():
                    r = self.api.post("/v1/cargoes-label/create",
                                      {"supply_id": int(s["supply_id"]), "cargoes": [{"cargo_id": int(i)} for i in ids]})
                    if (r.get("errors") or {}).get("error_reasons"):
                        raise OzonError("Этикетки: " + ", ".join(r["errors"]["error_reasons"]))
                    ops.append([s, gid, r.get("operation_id")])
            except OzonError as e:
                ops = [o for o in ops if o[0] is not s]
                self.ship_error(s, e)
        if ops:
            self.log(f"Запрошены этикетки: {len(ops)} файлов по {len({id(o[0]) for o in ops})} поставкам — Озон готовит их параллельно")
        t0 = time.time()
        while ops:
            self.check_stop()
            for o in list(ops):
                s, gid, op = o
                if s["cluster_id"] in self.failed_now:
                    ops.remove(o)
                    continue
                try:
                    g = self.api.post("/v1/cargoes-label/get", {"operation_id": op})
                    st = g.get("status")
                    if st in ("IN_PROGRESS", None, ""):
                        continue
                    ops.remove(o)
                    res = g.get("result") or {}
                    if st != "SUCCESS" or not (res.get("file_url") or res.get("file_guid")):
                        raise OzonError("Этикетки не сформированы: " + ", ".join(
                            (g.get("errors") or {}).get("error_reasons") or [st or ""]))
                    url = res.get("file_url") or f"https://api-seller.ozon.ru/v1/cargoes-label/file/{res['file_guid']}"
                    pdf = self.api.get_file(url)
                    if not pdf.startswith(b"%PDF"):
                        raise OzonError("Озон вернул не PDF-файл этикеток")
                    rel = f"raw/{s['supply_id']}_{gid}.pdf"
                    with open(os.path.join(self.dir, rel), "wb") as f:
                        f.write(pdf)
                    s["labels"][gid] = rel
                    self.save()
                    if not any(x[0] is s for x in ops):
                        self.log("Этикетки получены", s)
                except OzonError as e:
                    ops = [x for x in ops if x[0] is not s]
                    self.ship_error(s, e)
            if ops:
                if time.time() - t0 > 900:
                    for s in {id(o[0]): o[0] for o in ops}.values():
                        self.ship_error(s, OzonError("Озон долго готовит этикетки — нажмите «Продолжить» позже"))
                    break
                self.api.sleep(3 * self.api.speed)

    # --------------------------------------------------------- пропуск
    def make_pass(self, order_id, holder, log_ship=None):
        ps = self.batch.get("pass") or {}
        if not ps.get("enabled") or holder.get("pass_done"):
            return
        self.check_stop()
        body = {"supply_order_id": int(order_id), "vehicle": {
            "driver_name": ps.get("driver_name", ""), "driver_phone": ps.get("driver_phone", ""),
            "vehicle_model": ps.get("vehicle_model", ""), "vehicle_number": ps.get("vehicle_number", "")}}
        try:
            r = self.api.post("/v1/supply-order/pass/create", body)
            errs = [e for e in r.get("error_reasons") or [] if e not in ("UNSPECIFIED",)]
            if errs:
                raise OzonError(", ".join(errs))
            if r.get("operation_id"):
                st = self.api.poll("/v1/supply-order/pass/status", {"operation_id": r["operation_id"]},
                                   lambda x: str(x.get("result") or "").upper() not in ("", "IN_PROGRESS", "UNSPECIFIED"),
                                   interval=2, timeout=120)
                if st.get("errors"):
                    raise OzonError(", ".join(map(str, st["errors"])))
            holder["pass_done"] = True
            self.log("Пропуск оформлен", log_ship)
        except OzonError as e:
            msg = f"Пропуск не оформлен: {e}"
            holder.setdefault("warn", []).append(msg)
            holder["pass_done"] = "failed"
            self.log(msg, log_ship, "warn")
        self.save()

    # --------------------------------------------------------- общий ход
    def run(self):
        b = self.batch
        b["error"] = None
        if b.get("multi"):
            return self.run_multi()
        ships = self.active()

        # 1. Черновики
        if not b.get("chosen_slot"):
            b["phase"] = "drafts"
            self.save()
            for s in ships:
                if s.get("order_id"):
                    continue
                if self.draft_alive(s) and s.get("slots") is not None and s["status"] == "draft_ok":
                    continue
                self.safe(s, self.create_draft)
            ok = [s for s in ships if s.get("draft_id") and s["status"] == "draft_ok"]
            if not ok:
                return self.fail("Не удалось создать ни одного черновика")
            if not self.pick_slot([s["slots"] for s in ok], min(s["draft_at"] for s in ok)):
                return

        # 2. Заявки — сначала все подряд, пока живы черновики
        b["phase"] = "supplies"
        self.save()
        for s in ships:
            if s.get("order_id") or s["cluster_id"] in self.failed_now or s["status"] == "done":
                continue
            self.safe(s, self.create_supply)
        for s in ships:
            if s.get("order_id") and not s.get("supply_id") and s["cluster_id"] not in self.failed_now:
                self.safe(s, lambda x: self.fetch_order(x["order_id"], [x]))

        # 3. Грузоместа, этикетки, пропуска
        b["phase"] = "cargo"
        self.save()
        self.cargo_phase(ships)
        self.finish()

    def pick_slot(self, slot_lists, oldest_draft_at):
        """Выбирает общий таймслот (авто) или останавливается и ждёт выбора пользователя."""
        b = self.batch
        common = S.common_slots(slot_lists)
        b["common_slots"] = common
        if b["mode"] == "auto":
            pref = b["slot_pref"]
            cov = {S.key(sl): n for sl, n in S.slot_counts(slot_lists)}
            plan = [S.choose_for_city(l, pref, cov) for l in slot_lists]
            if not any(plan):
                self.fail("У Озона нет свободных таймслотов")
                return False
            # для общей заявки слот один; для отдельных — запоминаем самый частый для отображения
            b["chosen_slot"] = max((p for p in plan if p), key=lambda p: (sum(1 for q in plan if q == p), -S.parse(p[0]).timestamp()))
            b["chosen_slot_human"] = S.summary(plan)
            inside = sum(1 for p in plan if p and S.distance(p, pref) == 0)
            if len(slot_lists) == 1:
                self.log(f"Выбран таймслот {S.human(plan[0])}" + ("" if inside else " (в диапазоне свободных нет — ближайший)"),
                         lvl="info" if inside else "warn")
            else:
                self.log(f"Таймслоты по городам: в диапазоне — {inside} из {len(plan)}, остальным ближайшие. {S.summary(plan)}",
                         lvl="info" if inside == len(plan) else "warn")
            self.save()
            return True
        b["phase"] = "await_slot"
        b["slot_deadline"] = oldest_draft_at + DRAFT_TTL
        self.log("Черновики готовы — выберите таймслот")
        self.save()
        return False

    def fail(self, msg):
        self.batch["phase"] = "error"
        self.batch["error"] = msg
        self.log(msg, lvl="error")
        self.save()

    def finish(self):
        b = self.batch
        self.rebuild_outputs()
        bad = [s for s in self.active() if s["status"] != "done"]
        b["phase"] = "done" if not bad else "partial"
        self.log("Готово" if not bad else f"Готово с ошибками: {len(bad)} поставок требуют внимания",
                 lvl="info" if not bad else "warn")
        self.save()

    # ================================================================ ОДНА ЗАЯВКА НА ВСЕ ГОРОДА
    def run_multi(self):
        b = self.batch
        M = b.setdefault("order", {})        # общая заявка: draft_id, draft_at, slots, order_id, pass_done
        ships = self.active()

        if not M.get("order_id"):
            if not b.get("chosen_slot") or not self.draft_alive(M):
                b["phase"] = "drafts"
                self.save()
                try:
                    self.create_multi_draft(M)
                except OzonError as e:
                    if self.stop.is_set():
                        raise
                    return self.fail(str(e))
                if not b.get("chosen_slot"):
                    if not self.pick_slot([M["slots"]], M["draft_at"]):
                        return
            elif time.time() - M.get("slots_at", 0) > 90:
                M["slots"] = self.fetch_slots(M["draft_id"], "MULTI_CLUSTER", self.multi_ships())
                M["slots_at"] = time.time()

            b["phase"] = "supplies"
            self.save()
            live = self.multi_ships()
            slot = self.take_slot(M["slots"], M)
            for s in live:
                self.set(s, "supply")
            try:
                M["order_id"] = self.submit_supply(M["draft_id"], "MULTI_CLUSTER", live, slot)
            except OzonError as e:
                for s in live:
                    self.set(s, "error", str(e))
                return self.fail(str(e))
            M["slot"] = slot
            b["slot_locked"] = True
            for s in live:
                s["order_id"] = M["order_id"]
                s["slot"] = slot
            self.log(f"Заявка создана, id {M['order_id']}, {len(live)} городов, таймслот {S.human(slot)}")

        live = [s for s in ships if s.get("order_id")]
        if any(not s.get("supply_id") for s in live):
            try:
                self.fetch_order(M["order_id"], live)
            except OzonError as e:
                if self.stop.is_set():
                    raise
                for s in live:
                    if not s.get("supply_id"):
                        self.set(s, "error", str(e))

        b["phase"] = "cargo"
        self.save()
        self.cargo_phase(live)
        if any(s["status"] == "done" for s in live):
            self.make_pass(M["order_id"], M)
        self.finish()

    def multi_ships(self):
        did = (self.batch.get("order") or {}).get("draft_id")
        return [s for s in self.active() if not s.get("excluded") and s.get("draft_id") == did]

    def create_multi_draft(self, M):
        """Черновик на несколько кластеров. Кластеры, которые Озон не принимает, исключаются."""
        ships = [s for s in self.active() if not s.get("excluded")]
        for attempt in range(3):
            self.check_stop()
            if not ships:
                raise OzonError("Не осталось ни одного города, который Озон принимает")
            if len(ships) > 20:
                raise OzonError("Одной заявкой можно отправить не больше 20 кластеров")
            for s in ships:
                self.set(s, "draft")
            body = {"clusters_info": [{"macrolocal_cluster_id": int(s["cluster_id"]), "items": self.items_of(s)}
                                      for s in ships],
                    "deletion_sku_mode": "FULL", "delivery_info": self.delivery_info()}
            r = self.api.post("/v1/draft/multi-cluster/create", body)
            bad = _bad_clusters(r.get("errors"))
            draft_id = r.get("draft_id")
            info = self.poll_draft(draft_id) if draft_id else {}
            bad |= _bad_clusters(info.get("errors"))
            if draft_id and info.get("status") == "SUCCESS" and not bad:
                break
            if not bad:
                raise OzonError("Черновик не создан: " + (_errors_text(info.get("errors") or r.get("errors"))
                                                          or info.get("status", "нет draft_id")))
            for s in ships:
                if str(s["cluster_id"]) in bad:
                    s["excluded"] = True
                    self.set(s, "error", REASONS["NOT_AVAILABLE_CLUSTERS"])
                    self.log(REASONS["NOT_AVAILABLE_CLUSTERS"] + " — город исключён из заявки", s, "warn")
            ships = [s for s in ships if not s.get("excluded")]
        else:
            raise OzonError("Не удалось создать черновик на несколько кластеров")

        by_id = {str(c.get("macrolocal_cluster_id")): c for c in info.get("clusters") or []}
        rejected = _rejected(r.get("errors"))
        rejected.update(_rejected(info.get("errors")))
        for s in ships:
            cl = by_id.get(str(s["cluster_id"]))
            try:
                self.check_cluster(s, cl)
            except OzonError as e:
                self.set(s, "error", str(e))
                raise
            s["storage_wh"] = _best_warehouse(cl)
            if rejected:
                self.apply_rejected(s, {k: v for k, v in rejected.items() if k in s["items"]})
            s["draft_id"] = draft_id
            self.set(s, "draft_ok")
        M["draft_id"] = draft_id
        M["draft_at"] = time.time()
        M["slots"] = self.fetch_slots(draft_id, "MULTI_CLUSTER", ships)
        M["slots_at"] = time.time()
        self.log(f"Черновик {draft_id} на {len(ships)} кластеров: {len(M['slots'])} таймслотов")

    # --------------------------------------------------------- обёртки
    def safe(self, s, fn):
        try:
            fn(s)
        except OzonError as e:
            if self.stop.is_set():
                self.set(s, s["status"], "остановлено")
                raise
            self.failed_now.add(s["cluster_id"])
            self.set(s, "error", str(e))
            self.log(str(e), s, "error")
        except Exception as e:  # неожиданная ошибка — пишем подробно в api.log
            self._api_log(traceback.format_exc())
            self.failed_now.add(s["cluster_id"])
            self.set(s, "error", f"Внутренняя ошибка: {e}")
            self.log(f"Внутренняя ошибка: {e}", s, "error")

    def rebuild_outputs(self):
        b = self.batch
        act = self.active()
        names = {s["cluster_id"]: s["name"] for s in act}
        b["groups"] = P.group_boxes({s["cluster_id"]: s["boxes"] for s in act}, b["products"],
                                    [s["cluster_id"] for s in act], names)
        b["files"] = L.build_outputs(b, self.dir)
        self.save()


# ------------------------------------------------------------------ запуск
def start(pid, bid):
    if bid in RUNNING and RUNNING[bid][0].is_alive():
        return False
    stop = threading.Event()

    def target():
        r = None
        try:
            r = Run(pid, bid, stop)
            r.run()
        except OzonError as e:
            if r:
                r.batch["phase"] = "stopped" if stop.is_set() else "error"
                r.batch["error"] = str(e)
                r.log(str(e), lvl="warn" if stop.is_set() else "error")
        except Exception as e:
            if r:
                r._api_log(traceback.format_exc())
                r.batch["phase"] = "error"
                r.batch["error"] = f"Внутренняя ошибка: {e}"
                r.save()
        finally:
            if r:
                r._apilog.close()

    t = threading.Thread(target=target, daemon=True)
    RUNNING[bid] = (t, stop)
    t.start()
    return True


def stop(bid):
    if bid in RUNNING:
        RUNNING[bid][1].set()


def is_running(bid):
    return bid in RUNNING and RUNNING[bid][0].is_alive()


def _batch_api(pid, bid):
    profile = ST.get_profile(pid)
    lf = open(os.path.join(ST.batch_dir(pid, bid), "api.log"), "a", encoding="utf-8")
    api = make_client(profile, log=lambda s: (lf.write(time.strftime("%H:%M:%S ") + s + "\n"), lf.flush()))
    return api, lf


def cluster_names(api):
    """macrolocal_cluster_id → название (официальный список макролокальных кластеров)."""
    out = {}
    r = api.post("/v2/cluster/list", {})
    for x in r.get("result") or []:
        cid = x.get("macrolocal_cluster_id")
        name = (((x.get("data") or {}).get("macrolocal_cluster") or {}).get("name"))
        if cid:
            out[str(cid)] = name or str(cid)
    return out


def sync_batch(pid, bid):
    """Сверяет созданные заявки с Озоном: в какой кластер на самом деле едет каждая поставка."""
    batch = ST.load_batch(pid, bid)
    api, lf = _batch_api(pid, bid)
    try:
        names = cluster_names(api)
        orders = sorted({int(s["order_id"]) for s in batch["shipments"] if s.get("order_id")})
        got = {}
        for i in range(0, len(orders), 50):
            r = api.post("/v3/supply-order/get", {"order_ids": orders[i:i + 50]})
            for o in r.get("orders") or []:
                got[str(o.get("order_id"))] = o
        bad = 0
        for s in batch["shipments"]:
            o = got.get(str(s.get("order_id")))
            if not o:
                continue
            s["order_number"] = o.get("order_number") or s.get("order_number")
            s["order_state"] = o.get("state")
            sups = o.get("supplies") or []
            sup = next((x for x in sups if str(x.get("supply_id")) == str(s.get("supply_id"))), None)
            if sup is None and len(sups) == 1:
                sup = sups[0]
            if sup:
                ml = str(sup.get("macrolocal_cluster_id") or "")
                s["actual_cluster"] = names.get(ml, ml)
                s["mismatch"] = bool(ml) and not (ml == str(s["cluster_id"]) and same_cluster(s.get("cluster_name"), s["actual_cluster"])) \
                    and not same_cluster(s["name"], s["actual_cluster"])
                bad += bool(s["mismatch"])
            if o.get("state") == "CANCELLED":
                s["cancelled"] = True
                s["status"] = "cancelled"
        batch["synced_at"] = time.strftime("%d.%m %H:%M")
        batch.setdefault("log", []).append({"t": time.strftime("%H:%M:%S"), "city": "",
                                            "msg": f"Сверка с Озоном: {bad} поставок едут не в тот кластер" if bad
                                            else "Сверка с Озоном: все поставки едут в свои кластеры",
                                            "lvl": "warn" if bad else "info"})
        ST.save_batch(pid, batch)
        return bad
    finally:
        lf.close()


RETRY_CANCEL = {"OTHER_ASYNCHRONOUS_OPERATION_IN_PROGRESS"}


def start_cancel(pid, bid, cluster_ids=None):
    """Отмена заявок в фоне. Если поставки ещё создаются — сначала останавливает создание."""
    if bid in RUNNING and RUNNING[bid][0].is_alive():
        RUNNING[bid][1].set()
        RUNNING[bid][0].join(timeout=90)          # текущий запрос к Озону может идти до минуты
        if RUNNING[bid][0].is_alive():
            raise OzonError("Не удалось остановить создание поставок — попробуйте ещё раз через минуту")
    stop_ev = threading.Event()
    batch = ST.load_batch(pid, bid)
    batch["phase_before_cancel"] = batch.get("phase")
    batch["phase"] = "cancelling"
    batch.pop("cancel_result", None)
    ST.save_batch(pid, batch)
    t = threading.Thread(target=_cancel_job, args=(pid, bid, cluster_ids), daemon=True)
    RUNNING[bid] = (t, stop_ev)
    t.start()
    return True


def _cancel_job(pid, bid, cluster_ids):
    batch = ST.load_batch(pid, bid)
    api, lf = _batch_api(pid, bid)

    def log(msg, city="", lvl="info"):
        batch.setdefault("log", []).append({"t": time.strftime("%H:%M:%S"), "city": city, "msg": msg, "lvl": lvl})
        ST.save_batch(pid, batch)

    try:
        targets = [s for s in batch["shipments"] if not s.get("cancelled")
                   and (not cluster_ids or s["cluster_id"] in cluster_ids)]
        orders = {}
        for s in targets:
            if s.get("order_id"):
                orders.setdefault(str(s["order_id"]), []).append(s)
                s["status_before_cancel"] = s.get("status")
                s["status"] = "cancelling"
            else:                                   # заявки в Озоне нет — отменять нечего
                s["cancelled"] = True
                s["status"] = "cancelled"
                s["error"] = None
        ST.save_batch(pid, batch)
        log(f"Отменяю заявки в Озоне: {len(orders)}")

        result = {}                                 # order_id -> (ok, причина)
        pending = list(orders)
        for attempt in range(3):
            ops = {}
            for oid in pending:                      # сначала отправляем все отмены сразу
                try:
                    r = api.post("/v1/supply-order/cancel", {"order_id": int(oid)})
                    ops[oid] = r.get("operation_id")
                except OzonError as e:
                    result[oid] = (False, str(e))
            t0 = time.time()
            while ops and time.time() - t0 < 300:    # потом собираем ответы
                for oid, op in list(ops.items()):
                    try:
                        st = api.post("/v1/supply-order/cancel/status", {"operation_id": op})
                    except OzonError as e:
                        result[oid] = (False, str(e))
                        ops.pop(oid)
                        continue
                    if st.get("status") in ("IN_PROGRESS", None, ""):
                        continue
                    ops.pop(oid)
                    if st.get("status") == "SUCCESS" or (st.get("result") or {}).get("is_order_cancelled"):
                        result[oid] = (True, "")
                    else:
                        reasons = st.get("error_reasons") or [st.get("status", "")]
                        result[oid] = (False, ", ".join(reasons), set(reasons) & RETRY_CANCEL)
                if ops:
                    api.sleep(3 * api.speed)
            for oid in ops:
                result[oid] = (False, "Озон долго не отвечает на отмену", True)
            pending = [oid for oid, r in result.items() if not r[0] and (len(r) < 3 or r[2])]
            if not pending:
                break
            log(f"Повторяю отмену для {len(pending)} заявок (Озон был занят)", lvl="warn")
            api.sleep(15 * api.speed)

        # проверка: что Озон показывает на самом деле
        try:
            ids = [int(o) for o in orders]
            for i in range(0, len(ids), 50):
                r = api.post("/v3/supply-order/get", {"order_ids": ids[i:i + 50]})
                for o in r.get("orders") or []:
                    if o.get("state") == "CANCELLED":
                        result[str(o.get("order_id"))] = (True, "")
        except OzonError:
            pass

        ok_names, failed = [], []
        for oid, ships in orders.items():
            ok, err = result.get(oid, (False, "нет ответа"))[:2]
            for s in ships:
                if ok:
                    s["cancelled"] = True
                    s["status"] = "cancelled"
                    s["error"] = None
                    ok_names.append(s["name"])
                else:
                    s["status"] = s.pop("status_before_cancel", None) or "error"
                    s["error"] = f"Заявка не отменена: {_reasons(err.split(', ')) if err else ''}"
                    failed.append({"name": s["name"], "error": s["error"]})
                s.pop("status_before_cancel", None)
        skipped = [s["name"] for s in targets if not s.get("order_id")]
        batch["cancel_result"] = {"ok": ok_names, "skipped": skipped, "failed": failed, "shown": False,
                                  "at": time.strftime("%d.%m %H:%M")}
        all_cancelled = all(s.get("cancelled") for s in batch["shipments"])
        batch["phase"] = "cancelled" if all_cancelled else (
            "partial" if failed or batch.get("phase_before_cancel") in ("error", "partial", "stopped", "done", "cargo")
            else batch.get("phase_before_cancel") or "partial")
        if all_cancelled:
            batch["error"] = None
        log(f"Отменено заявок: {len(ok_names)}" + (f", не созданы и сняты: {len(skipped)}" if skipped else "")
            + (f", не удалось: {len(failed)}" if failed else ""),
            lvl="warn" if failed else "info")
    except Exception as e:
        lf.write(traceback.format_exc())
        batch["phase"] = "partial"
        batch["error"] = f"Отмена прервалась: {e}"
        batch["cancel_result"] = {"ok": [], "failed": [{"name": "", "error": str(e)}], "shown": False}
        ST.save_batch(pid, batch)
    finally:
        ST.save_batch(pid, batch)
        lf.close()


def cancel_orders(pid, bid, cluster_ids=None):
    """Синхронная отмена (для совместимости): запускает фоновую и ждёт её окончания."""
    start_cancel(pid, bid, cluster_ids)
    RUNNING[bid][0].join()
    b = ST.load_batch(pid, bid)
    res = b.get("cancel_result") or {}
    return [(n, True, "") for n in res.get("ok", [])] + [(f["name"], False, f["error"]) for f in res.get("failed", [])]


# ------------------------------------------------------------------ разбор ошибок
def _reasons(codes):
    return ", ".join(REASONS.get(c, c) for c in codes)


def _rejected(errors):
    out = {}
    for e in errors or []:
        for v in e.get("items_validation") or []:
            if v.get("sku"):
                out.setdefault(str(v["sku"]), []).extend(r for r in v.get("reasons") or [] if r != "UNSPECIFIED")
            for r in v.get("rejected_items") or []:
                if r.get("sku"):
                    out.setdefault(str(r["sku"]), []).extend(x for x in r.get("reasons") or [] if x != "UNSPECIFIED")
    return out


def _bad_clusters(errors):
    """Кластеры, которые Озон отказался принимать (NOT_AVAILABLE_CLUSTERS)."""
    out = set()
    for e in errors or []:
        if "NOT_AVAILABLE_CLUSTERS" in (e.get("error_reasons") or []) or e.get("error_message") == "NOT_AVAILABLE_CLUSTERS":
            out |= {str(x) for x in e.get("macrolocal_cluster_ids") or []}
    return out


def _errors_text(errors):
    parts = []
    for e in errors or []:
        for x in [e.get("error_message"), e.get("message")] + list(e.get("error_reasons") or []):
            if x and x != "UNSPECIFIED":
                parts.append(REASONS.get(str(x), str(x)))
        for sku, rs in _rejected([e]).items():
            parts.append(f"SKU {sku}: {', '.join(rs) or 'отклонён'}")
    return "; ".join(dict.fromkeys(parts))


def _cargo_errors(e):
    v = [f"{x.get('cargo_key', '')} {x.get('item') or x.get('barcode') or ''}: {x.get('type')}".strip()
         for x in e.get("items_validation") or []]
    reasons = [r for r in e.get("error_reasons") or [] if r not in ("ERROR_REASON_UNSPECIFIED", "UNSPECIFIED")]
    return "Озон отклонил грузоместа: " + "; ".join(reasons + v) if (reasons or v) else ""


def _best_warehouse(cl):
    if not cl:
        return None
    good = []
    for w in cl.get("warehouses") or []:
        st = str(((w.get("availability_status") or {}).get("state")) or "").upper()
        sw = w.get("storage_warehouse") or {}
        wid = sw.get("warehouse_id") or w.get("warehouse_id")
        if not wid or "NOT" in st or "UNAVAILABLE" in st:
            continue
        good.append((w.get("total_rank") or 9999, -(w.get("total_score") or 0), wid, sw.get("name") or w.get("name")))
    if good:
        good.sort()
        return {"id": good[0][2], "name": good[0][3]}
    return None
