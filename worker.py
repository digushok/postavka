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
RETRY_NO_SLOTS = 60          # как часто повторно спрашивать слоты у города, где их не было
WAIT_NO_SLOTS = 5 * 60       # сколько ещё ждать слотов после всех черновиков; потом — заявка без таймслота
RUNNING = {}                 # batch_id -> (thread, stop_event)
_save_lock = threading.Lock()

REASONS = {
    "NOT_AVAILABLE_CLUSTERS": "Озон не принимает поставки в этот кластер через выбранную точку отгрузки",
    "CAN_NOT_CREATE_DRAFT": "Озон не смог создать черновик",
    "TIMESLOT_NOT_AVAILABLE": "таймслот успели занять (Озон отдал его другому продавцу)",
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


def skey(s):
    """Уникальный ключ поставки в партии (кластер может повторяться после замены)."""
    return s.get("key") or str(s["cluster_id"])


REPLACE = {}                 # batch_id -> [(ключ, новый cluster_id, название, повтор?)] — замены во время работы
ADD = {}                     # batch_id -> [(поставки, товары)] — добавленные по номеру во время работы
MERGE = {}                   # batch_id -> [(ключ несозданной, ключ поставки-получателя, "keep"|"sum")]


def replaceable(s):
    """Поставку можно перенаправить в другой кластер: заявки в Озоне ещё нет, а создать её не удалось."""
    return not s.get("cancelled") and not s.get("order_id") and (s.get("status") == "error" or s.get("excluded"))


def apply_replacement(batch, key, cluster_id, cluster_name, retry=False):
    """Перенаправляет поставку в другой кластер (можно в уже выбранный — уйдёт отдельной заявкой).
    retry=True — повторная попытка в тот же кластер: название и кластер не меняются."""
    s = next((x for x in batch["shipments"] if skey(x) == key), None)
    if s is None:
        raise OzonError("Поставка не найдена")
    if s.get("order_id"):
        raise OzonError("По этой поставке заявка уже создана — сначала отмените её")
    old_name = s["name"]
    if retry:
        cluster_id, cluster_name = s["cluster_id"], s["name"]
    cluster_id = str(cluster_id)
    taken = {skey(x) for x in batch["shipments"] if x is not s}
    new_key, n = cluster_id, 2
    while new_key in taken:
        new_key, n = f"{cluster_id}#{n}", n + 1
    if s.get("items_orig"):                      # прежний кластер мог выкинуть часть SKU — возвращаем состав
        s["items"], s["boxes"] = dict(s["items_orig"]), list(s["boxes_orig"])
    kits = batch.get("kits")
    if kits and kits.get("alloc") and key in kits["alloc"]:
        kits["alloc"][new_key] = kits["alloc"].pop(key)
    if retry:
        s.update({"key": new_key, "status": "new", "error": None})
    else:
        s.setdefault("replaced_from", old_name)
        s.update({"cluster_id": cluster_id, "cluster_name": cluster_name, "name": cluster_name, "key": new_key,
                  "status": "new", "error": None})
    for f in ("draft_id", "draft_at", "slots", "slots_at", "storage_wh", "ozon_cluster_name", "tried_slots",
              "excluded", "actual_cluster", "mismatch"):
        s.pop(f, None)
    same = any(str(x["cluster_id"]) == cluster_id for x in batch["shipments"] if x is not s)
    note = ("Повторная попытка" if retry else f"Кластер заменён: «{old_name}» → «{cluster_name}»") \
        + (" (отдельной заявкой)" if same else "")
    s.setdefault("warn", []).append(note)
    batch.setdefault("log", []).append({"t": time.strftime("%H:%M:%S"), "city": cluster_name, "msg": note, "lvl": "info"})
    return s


def norm(name):
    return re.sub(r"[^0-9a-zа-я]+", "", str(name or "").lower().replace("ё", "е"))


def same_cluster(expected, got):
    a, b = norm(expected), norm(got)
    if not a or not b:
        return True          # сравнить не с чем
    return a == b or a in b or b in a


class NoSlots(OzonError):
    """Озон вернул пустой список таймслотов — у точки отгрузки нет мест (бывает временно)."""


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
        self.pipe = []            # конвейер: поставки, у которых идут грузоместа и этикетки

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
        s.setdefault("items_orig", dict(s["items"]))
        s.setdefault("boxes_orig", list(s["boxes"]))
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
        if self.no_slot_mode():
            s["slots"] = []
        else:
            s["slots"] = self.fetch_slots(draft_id, "CROSSDOCK", [s])
        s["slots_at"] = time.time()
        self.set(s, "draft_ok")
        self.log(f"Черновик {draft_id} — кластер «{s.get('ozon_cluster_name') or s['cluster_id']}»"
                 + ("" if self.no_slot_mode() else f", {len(s['slots'])} таймслотов"), s)

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
            err = r.get("error_reason")
            if err and err not in ("UNSPECIFIED",):
                raise OzonError(f"таймслоты: {REASONS.get(err, err)}")
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

    def no_slots_text(self):
        d = self.batch.get("dropoff") or {}
        where = f"на точке отгрузки «{d['name']}»" if d.get("name") else "на точке отгрузки"
        today = date.today()
        return (f"Озон не даёт свободных таймслотов {where} с {today:%d.%m} по {today + timedelta(days=27):%d.%m} — "
                f"у точки нет мест на этот объём")

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
            raise NoSlots(self.no_slots_text())
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
        tried = {S.key(x) for x in s.get("tried_slots") or []}
        slot = S.choose_for_city([x for x in s["slots"] if S.key(x) not in tried], pref, cov)
        if not slot:
            raise NoSlots(self.no_slots_text())
        if S.day_shift(slot, pref) != 0:
            msg = f"В желаемый день свободных слотов нет — взят ближайший {S.human(slot)}"
            s.setdefault("warn", []).append(msg)
            self.log(msg, s, "warn")
        return slot

    def create_supply(self, s):
        self.check_stop()
        if not self.draft_alive(s):
            if s.get("draft_id"):
                self.log("Черновик устарел — создаю заново", s)
            self.create_draft(s)
        elif time.time() - s.get("slots_at", 0) > 90:
            s["slots"] = self.fetch_slots(s["draft_id"], "CROSSDOCK", [s])
            s["slots_at"] = time.time()
        auto = self.batch.get("mode") == "auto"
        s["tried_slots"] = []
        for attempt in range(4):
            if auto:
                slot = self.auto_slot(s)
            else:
                avail = [x for x in s["slots"] if S.key(x) not in {S.key(t) for t in s["tried_slots"]}]
                slot = self.take_slot(avail, s)
            self.set(s, "supply")
            try:
                order_id = self.submit_supply(s["draft_id"], "CROSSDOCK", [s], slot, s)
                break
            except OzonError as e:
                if "TIMESLOT_NOT_AVAILABLE" not in getattr(e, "codes", ()) or attempt == 3:
                    raise
                # слот забрали между проверкой и бронированием — берём следующий
                s["tried_slots"].append(slot)
                self.log(f"Таймслот {S.human(slot)} успели занять — беру следующий свободный", s, "warn")
                s["slots"] = self.fetch_slots(s["draft_id"], "CROSSDOCK", [s])
                s["slots_at"] = time.time()
        s["order_id"] = order_id
        s["slot"] = slot
        if auto:
            if not self.batch.get("chosen_slot"):
                self.batch["chosen_slot"] = slot      # нужен заменам кластеров во время работы
            self.batch["chosen_slot_human"] = self.slot_summary()
        self.save()
        self.log(f"Заявка создана, id {order_id}, таймслот {S.human(slot)}", s)

    def no_slot_mode(self):
        """Режим «Без таймслота»: заявки сразу без слота, слоты у Озона даже не запрашиваются."""
        return self.batch.get("mode") == "none"

    def supply_fn(self):
        return self.create_supply_noslot if self.no_slot_mode() else self.create_supply

    def slot_summary(self):
        act = self.active()
        txt = S.summary([x.get("slot") for x in act if x.get("slot")])
        n = sum(1 for x in act if x.get("no_slot") and x.get("order_id"))
        if n:
            txt = (txt + " · " if txt else "") + f"без таймслота — {n}"
        return txt

    def create_supply_noslot(self, s):
        """Заявка без таймслота (как «Создать без таймслота» в кабинете) — слот выбирается потом в кабинете."""
        self.check_stop()
        if not self.draft_alive(s):
            self.create_draft(s)
        self.set(s, "supply")
        try:
            order_id = self.submit_supply(s["draft_id"], "CROSSDOCK", [s], None, s)
        except OzonError as e:
            if self.no_slot_mode():
                raise OzonError(f"Заявку без таймслота Озон не принял: {e}")
            raise OzonError(f"{self.no_slots_text()}. Заявку без таймслота Озон тоже не принял: {e}")
        s["order_id"] = order_id
        s["slot"] = None
        s["no_slot"] = True
        msg = "Заявка создана без таймслота — выберите таймслот в кабинете Озона"
        s.setdefault("warn", []).append(msg)
        self.batch["chosen_slot_human"] = self.slot_summary()
        self.save()
        self.log(f"Заявка создана, id {order_id}, без таймслота — выберите таймслот в кабинете Озона", s, "warn")

    def submit_supply(self, draft_id, supply_type, ships, slot, holder):
        """Отправляет заявку и ждёт её номер. Перед отправкой в holder (поставка или общая заявка) пишется
        «submitting»: если программу остановят, пока Озон создаёт заявку, при «Продолжить» программа
        сначала спросит Озон о судьбе этой заявки (recover_submits), а не создаст вторую."""
        body = {"draft_id": draft_id, "selected_cluster_warehouses": self.cluster_wh(ships),
                "supply_type": supply_type}
        if slot:                         # без слота — заявка «без таймслота», слот выбирается в кабинете
            body["timeslot"] = {"from_in_timezone": slot[0], "to_in_timezone": slot[1]}
        self.check_stop()
        holder["submitting"] = {"draft_id": draft_id, "slot": slot, "at": time.time()}
        self.save()
        flag, self.api.stop_flag = self.api.stop_flag, None     # от отправки до номера — без остановки
        try:
            r = self.api.post("/v2/draft/supply/create", body)
            errs = [e for e in (r.get("error_reasons") or []) if e not in ("UNSPECIFIED",)]
            if errs:
                holder.pop("submitting", None)
                e = OzonError("Заявка не создана: " + _reasons(errs))
                e.codes = errs
                raise e
            st = self.supply_status(draft_id)
        except OzonError as e:
            # Озон ответил отказом (4xx) — заявка точно не создана, отметку снимаем. Без ответа
            # (обрыв связи, 5xx) отметка остаётся: при «Продолжить» программа сначала спросит Озон.
            if e.status and 400 <= int(e.status) < 500 and e.status != 429:
                holder.pop("submitting", None)
            raise
        finally:
            self.api.stop_flag = flag
        holder.pop("submitting", None)
        if st.get("status") != "SUCCESS" or not st.get("order_id"):
            codes = [c for c in st.get("error_reasons") or [] if c != "UNSPECIFIED"] or [st.get("status", "")]
            e = OzonError("Заявка не создана: " + _reasons(codes))
            e.codes = codes
            raise e
        return st["order_id"]

    def supply_status(self, draft_id):
        return self.api.poll("/v2/draft/supply/create/status", {"draft_id": draft_id},
                             lambda x: x.get("status") not in ("IN_PROGRESS", None, "", "UNSPECIFIED"),
                             interval=3, timeout=300)

    def recover_submits(self):
        """Заявки, отправленные в Озон перед остановкой программы, но без полученного номера:
        спрашиваем Озон, создана ли заявка по тому черновику. Создана — берём её номер, не создана —
        можно отправлять заново. Не удалось узнать — останавливаемся, чтобы не создать вторую."""
        b = self.batch
        M = b.get("order") or {}
        holders = ([M] if b.get("multi") and M.get("submitting") and not M.get("order_id") else []) + \
                  [s for s in self.active() if s.get("submitting") and not s.get("order_id")]
        for h in holders:
            sub = h["submitting"]
            ship = h if "name" in h else None
            try:
                st = self.supply_status(sub["draft_id"])
            except OzonError as e:
                if self.stop.is_set():
                    raise
                if e.status in (400, 404):
                    # «no active order creation process» — Озон не создавал заявку по этому черновику
                    h.pop("submitting", None)
                    self.log("Заявка, отправленная раньше, Озоном не создана — отправляю заново", ship)
                    self.save()
                    continue
                sub["tries"] = sub.get("tries", 0) + 1
                if sub["tries"] < 3:
                    self.save()
                    raise OzonError(f"Перед остановкой программы заявка{' в ' + ship['name'] if ship else ''} "
                                    f"уже отправлялась в Озон, а узнать, создана ли она, не удалось ({e}). "
                                    f"Нажмите «Продолжить» ещё раз через минуту")
                h.pop("submitting", None)
                msg = ("Не удалось узнать у Озона, создалась ли заявка, отправленная перед остановкой программы, — "
                       "отправляю заново. Проверьте в кабинете Озона, нет ли двух заявок в этот кластер")
                (ship or {}).setdefault("warn", []).append(msg)
                self.log(msg, ship, "warn")
                continue
            h.pop("submitting", None)
            if st.get("status") == "SUCCESS" and st.get("order_id"):
                slot = sub.get("slot")
                if ship:
                    ship["order_id"], ship["slot"] = st["order_id"], slot
                    if not slot:
                        ship["no_slot"] = True
                    self.set(ship, "supply")
                else:
                    M["order_id"], M["slot"] = st["order_id"], slot
                    b["slot_locked"] = True
                    for s in self.multi_ships():
                        s["order_id"], s["slot"], s["no_slot"] = st["order_id"], slot, not slot
                self.log(f"Заявка была создана до остановки программы — номер получен у Озона: {st['order_id']}", ship)
            else:
                self.log("Заявка, отправленная перед остановкой программы, Озоном не создана — отправляю заново",
                         ship, "warn")
            self.save()

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

    # --------------------------------------------------------- конвейер: грузоместа → этикетки
    #
    # Каждая созданная заявка сразу попадает в конвейер. Пока программа ждёт, когда Озон разрешит
    # создать следующую заявку (не чаще 2 в минуту), конвейер двигает уже созданные поставки:
    # номер поставки → грузоместа → один запрос этикеток на всю поставку → страницы раскладываются
    # по файлам составов (файлы обновляются сразу, их можно печатать не дожидаясь конца).

    def pipe_add(self, s):
        if s["status"] == "done" or skey(s) in self.failed_now or any(it["s"] is s for it in self.pipe):
            return
        self.pipe.append({"s": s, "stage": "order", "op": None, "chunks": None, "t": time.time()})

    def pipe_busy(self):
        return bool(self.pipe)

    def pipe_tick(self):
        """Один шаг по каждой поставке в конвейере. Ничего не ждёт дольше одного запроса."""
        for it in list(self.pipe):
            self.check_stop()
            s = it["s"]
            try:
                self._pipe_step(it)
            except OzonError as e:
                if self.stop.is_set():
                    raise
                self.pipe.remove(it)
                self.ship_error(s, e)
            if it.get("stage") == "done" and it in self.pipe:
                self.pipe.remove(it)

    def pipe_drain(self):
        while self.pipe or self.take_replacements():
            self.pipe_tick()
            if self.pipe:
                self.api.sleep(2 * self.api.speed)

    def take_replacements(self):
        """Применяет замены кластеров, выбранные пользователем во время работы, и запускает эти поставки."""
        queue = REPLACE.pop(self.bid, [])
        started = False
        for src_key, dst_key, mode in MERGE.pop(self.bid, []):
            try:
                if self.merge_into(src_key, dst_key, mode):
                    started = True
            except OzonError as e:
                if self.stop.is_set():
                    raise
                self.log(f"Объединить не удалось: {e}", lvl="error")
        for ships, prods in ADD.pop(self.bid, []):
            for s in merge_import(self.batch, ships, prods):
                self.save()
                self.pipe_add(s)
                started = True
        for key, cid, name, retry in queue:
            try:
                s = apply_replacement(self.batch, key, cid, name, retry)
            except OzonError as e:
                self.log(str(e), lvl="error")
                continue
            self.failed_now.discard(key)
            self.failed_now.discard(skey(s))
            self.save()
            multi_pending = self.batch.get("multi") and not (self.batch.get("order") or {}).get("order_id")
            ready = self.batch.get("chosen_slot") or (self.batch.get("mode") in ("auto", "none") and not self.batch.get("multi"))
            if ready and not multi_pending:
                self.safe(s, self.supply_fn())
                if s.get("order_id"):
                    self.pipe_add(s)
                    started = True
        return started

    # --------------------------------------------------------- объединить несозданный кластер с другим
    def merge_into(self, src_key, dst_key, mode):
        """Товары поставки, которую Озон не принял (src), уходят в другую поставку партии (dst).
        mode "keep" — грузоместа src добавляются как есть; "sum" — одинаковый товар складывается
        в одно новое грузоместо. Если у dst заявка уже есть — меняем её состав в Озоне
        (/v1/supply-order/content/update); если Озон не даёт — отменяем заявку dst и создаём новую."""
        b = self.batch
        src = next((x for x in b["shipments"] if skey(x) == src_key and not x.get("cancelled")), None)
        dst = next((x for x in b["shipments"] if skey(x) == dst_key and not x.get("cancelled")), None)
        if not src or not dst or src is dst:
            raise OzonError("Поставка не найдена")
        if src.get("order_id"):
            raise OzonError(f"По «{src['name']}» заявка уже создана — объединять можно только несозданную")
        add_items = dict(src.get("items_orig") or src["items"])
        add_boxes = list(src.get("boxes_orig") or src["boxes"])
        items = dict(dst["items"])
        for sku, q in add_items.items():
            items[sku] = items.get(sku, 0) + int(q)
        boxes = [dict(x) for x in dst["boxes"]]
        n = 0

        def new_key(sku, qty):
            nonlocal n
            n += 1
            return f"m{len(dst.get('merged', []))}{n:02d}-{sku}-{qty}"
        changed = []                                   # ключи грузомест dst, которые меняются
        for bx in add_boxes:
            sku, qty = str(bx["sku"]), int(bx["qty"])
            mine = [x for x in boxes if str(x["sku"]) == sku]
            if mode == "sum" and mine:
                t = min(mine, key=lambda x: x["qty"])  # к самому маленькому грузоместу этого товара
                changed.append(t["key"])
                boxes.remove(t)
                boxes.append({"key": new_key(sku, t["qty"] + qty), "sku": sku, "qty": t["qty"] + qty})
            else:
                boxes.append({"key": new_key(sku, qty), "sku": sku, "qty": qty})
        what = (f"«{src['name']}» → «{dst['name']}»: +{sum(add_items.values())} шт, "
                + ("грузоместа как были" if mode == "keep" else "одинаковые товары в одном грузоместе"))
        src.update({"cancelled": True, "status": "merged", "error": None, "merged_into": dst["name"]})
        src.setdefault("warn", []).append(f"Объединена с «{dst['name']}»")
        dst.setdefault("merged", []).append({"from": src["name"], "items": add_items, "mode": mode})
        dst.setdefault("warn", []).append(f"Добавлены товары из «{src['name']}» ({sum(add_items.values())} шт)")
        self.failed_now.discard(skey(dst))

        if not dst.get("order_id"):                    # заявки у получателя ещё нет — просто новый состав
            dst["items"], dst["boxes"] = items, boxes
            dst.pop("items_orig", None)
            dst.pop("boxes_orig", None)
            self.log(f"Объединено: {what}", dst)
            self.save()
            return False

        live_cargo = [k for k in dst.get("cargo_ids") or {} if k not in changed]
        try:
            self.update_content(dst, items)
        except OzonError as e:
            if self.stop.is_set():
                raise
            if b.get("multi") and str(dst["order_id"]) == str((b.get("order") or {}).get("order_id")):
                src.update({"cancelled": False, "status": "error", "error": f"Объединить не удалось: {e}"})
                dst["merged"].pop()
                dst["warn"].pop()
                self.save()
                raise OzonError(f"Озон не дал изменить состав общей заявки ({e}). Отменять её ради одного "
                                f"кластера программа не стала — выберите «Заменить кластер» другим способом")
            self.log(f"Озон не дал изменить состав заявки «{dst['name']}» ({e}) — отменяю её и создаю новую "
                     f"с общим составом", dst, "warn")
            self.recreate_order(dst, items, boxes)
            dst.setdefault("warn", []).append(
                "Заявка пересоздана с общим составом: новый номер, таймслот и ВСЕ этикетки этой поставки — "
                "перепечатайте их, старые не годятся")
            self.save()
            self.safe(dst, self.supply_fn())
            if dst.get("order_id"):
                self.pipe_add(dst)
            return bool(dst.get("order_id"))

        dst["items"], dst["boxes"] = items, boxes
        if mode == "sum":
            dst["cargo_ids"] = {}                      # грузоместа заменены — создаём все заново
        else:
            dst["cargo_ids"] = {k: v for k, v in (dst.get("cargo_ids") or {}).items() if k in live_cargo}
        for f in ("labels", "cargo_pdf", "verified", "labels_per_cargo"):
            dst.pop(f, None)
        self.set(dst, "cargo")
        note = ("Грузоместа этой поставки созданы заново — перепечатайте все её этикетки" if mode == "sum" else
                "Добавлены новые грузоместа — допечатайте их этикетки (файлы составов обновятся)")
        dst["warn"].append(note)
        self.log(f"Объединено: {what}. Состав заявки в Озоне изменён. {note}", dst)
        self.save()
        self.pipe_add(dst)
        return True

    def update_content(self, s, items):
        body = {"order_id": int(s["order_id"]), "supply_id": int(s["supply_id"]),
                "items": [{"sku": int(k), "quantity": int(v), "quant": 1} for k, v in items.items() if int(v) > 0]}
        r = self.api.post("/v1/supply-order/content/update", body)
        errs = r.get("errors") or r.get("error_reasons") or []
        if errs and not r.get("operation_id"):
            raise OzonError("Состав не изменён: " + ", ".join(map(str, errs)))
        st = self.api.poll("/v1/supply-order/content/update/status", {"operation_id": r["operation_id"]},
                           lambda x: str(x.get("status") or "").upper() not in ("", "IN_PROGRESS", "UNSPECIFIED"),
                           interval=3, timeout=300)
        if "SUCCESS" not in str(st.get("status") or "").upper():
            raise OzonError("Состав не изменён: " + ", ".join(map(str, st.get("errors") or [st.get("status")])))

    def recreate_order(self, s, items, boxes):
        """Отменяет заявку поставки и готовит её к созданию заново с новым составом."""
        oid = int(s["order_id"])
        r = self.api.post("/v1/supply-order/cancel", {"order_id": oid})
        st = self.api.poll("/v1/supply-order/cancel/status", {"operation_id": r.get("operation_id")},
                           lambda x: x.get("status") not in ("IN_PROGRESS", None, ""), interval=3, timeout=300)
        if not (st.get("status") == "SUCCESS" or (st.get("result") or {}).get("is_order_cancelled")):
            raise OzonError("Не удалось отменить прежнюю заявку: " + ", ".join(st.get("error_reasons") or [str(st.get("status"))]))
        s.setdefault("replaced_orders", []).append(oid)
        for f in ("order_id", "order_number", "order_state", "supply_id", "storage_name", "cargo_ids", "labels",
                  "cargo_pdf", "verified", "labels_per_cargo", "slot", "no_slot", "draft_id", "draft_at", "slots",
                  "slots_at", "tried_slots", "pass_done", "items_orig", "boxes_orig", "submitting"):
            s.pop(f, None)
        s["items"], s["boxes"] = items, boxes
        s.update({"status": "new", "error": None})
        self.log(f"Прежняя заявка {oid} отменена", s, "warn")

    def idle_until(self, path):
        """Вместо простого ожидания лимита Озона — двигаем конвейер."""
        while True:
            left = self.api.time_until(path)
            if left <= 0 or not self.pipe:
                return
            t0 = time.time()
            self.pipe_tick()
            spent = time.time() - t0
            if spent < 1.5 and self.api.time_until(path) > 0:
                self.api.sleep(min(2.0, self.api.time_until(path)))

    def _pipe_step(self, it):
        s = it["s"]
        st = it["stage"]
        too_long = time.time() - it["t"] > 900

        if st == "order":
            if s.get("supply_id"):
                it["stage"] = "cargo"
                return
            r = self.api.post("/v3/supply-order/get", {"order_ids": [int(s["order_id"])]})
            o = (r.get("orders") or [None])[0]
            if o:
                s["order_number"] = o.get("order_number") or s.get("order_number")
                for sup in o.get("supplies") or []:
                    ml = str(sup.get("macrolocal_cluster_id") or "")
                    single = len(o.get("supplies") or []) == 1 and not self.batch.get("multi")
                    if sup.get("supply_id") and (ml == str(s["cluster_id"]) or (single and not ml)):
                        s["supply_id"] = sup["supply_id"]
                        s["storage_name"] = (sup.get("storage_warehouse") or {}).get("name")
                    elif single and ml and ml != str(s["cluster_id"]):
                        raise OzonError(f"Озон создал поставку в другой кластер (ID {ml}) — отмените эту заявку")
                self.save()
            if s.get("supply_id"):
                it["stage"] = "cargo"
            elif too_long:
                raise OzonError("Озон долго не выдаёт номер поставки — нажмите «Продолжить» позже")
            return

        if st == "cargo":
            s.setdefault("cargo_ids", {})
            if it["chunks"] is None:
                todo = [b for b in s["boxes"] if str(b["key"]) not in s["cargo_ids"]]
                it["chunks"] = P.chunks(todo)
                it["first"] = not s["cargo_ids"]
                it["n"] = 0
            if not it["chunks"]:
                missing = [b for b in s["boxes"] if str(b["key"]) not in s["cargo_ids"]]
                if missing:
                    raise OzonError(f"Озон не вернул номера для {len(missing)} грузомест")
                if it.get("n"):
                    self.log(f"Грузоместа созданы: {len(s['boxes'])}", s)
                it["stage"] = "label"
                return
            self.set(s, "cargo")
            prod = {p["sku"]: p for p in self.batch["products"]}
            part = it["chunks"].pop(0)
            cargoes = []
            for b in part:
                offer = prod.get(b["sku"], {}).get("offer_id")
                if not offer:
                    raise OzonError(f"У SKU {b['sku']} нет артикула (offer_id) — обновите данные товара в настройках")
                cargoes.append({"key": b["key"], "value": {"type": "BOX", "items": [
                    {"offer_id": offer, "quantity": int(b["qty"])}]}})
            r = self.api.post("/v1/cargoes/create", {"supply_id": int(s["supply_id"]),
                                                     "delete_current_version": bool(it["first"] and it["n"] == 0),
                                                     "cargoes": cargoes})
            it["n"] += 1
            e = r.get("errors") or {}
            if e.get("error_reasons") or e.get("items_validation"):
                raise OzonError(_cargo_errors(e))
            it["op"] = r.get("operation_id")
            it["stage"] = "cargo_wait"
            return

        if st == "cargo_wait":
            info = self.api.post("/v2/cargoes/create/info", {"operation_id": it["op"]})
            status = info.get("status")
            if status in ("IN_PROGRESS", None, "", "STATUS_UNSPECIFIED"):
                if too_long:
                    raise OzonError("Озон долго создаёт грузоместа — нажмите «Продолжить» позже")
                return
            if status != "SUCCESS":
                raise OzonError(_cargo_errors(info.get("errors") or {}) or f"Грузоместа: {status}")
            for c in (info.get("result") or {}).get("cargoes") or []:
                cargo_id = (c.get("value") or {}).get("cargo_id")
                if c.get("key") and cargo_id:
                    s["cargo_ids"][str(c["key"])] = cargo_id
            self.save()
            it["stage"] = "cargo"
            return

        if st == "label":
            s.setdefault("labels", {})
            if not s.get("verified"):
                if not self.verify_contents(s, it):
                    return                    # Озон ещё считает составы — проверим на следующем шаге
            groups = self.groups_of(s)
            if all(g in s["labels"] and os.path.exists(os.path.join(self.dir, s["labels"][g])) for g in groups):
                it["stage"] = "pass"
                return
            self.set(s, "labels")
            if s.get("labels_per_cargo") or self.batch.get("labels_mode") in ("per_group", "per_cargo"):
                # самый надёжный способ: по одному запросу на каждое грузоместо
                it["cops"] = {}
                s.setdefault("cargo_pdf", {})
                for b in s["boxes"]:
                    key = str(b["key"])
                    if key in s["cargo_pdf"] and os.path.exists(os.path.join(self.dir, s["cargo_pdf"][key])):
                        continue
                    cid = int(s["cargo_ids"][key])
                    r = self.api.post("/v1/cargoes-label/create",
                                      {"supply_id": int(s["supply_id"]), "cargoes": [{"cargo_id": cid}]})
                    if (r.get("errors") or {}).get("error_reasons"):
                        raise OzonError("Этикетки: " + ", ".join(r["errors"]["error_reasons"]))
                    it["cops"][key] = r.get("operation_id")
                it["stage"] = "label_wait_cargo"
                return
            ids = [int(s["cargo_ids"][str(b["key"])]) for b in s["boxes"]]
            r = self.api.post("/v1/cargoes-label/create",
                              {"supply_id": int(s["supply_id"]), "cargoes": [{"cargo_id": i} for i in ids]})
            if (r.get("errors") or {}).get("error_reasons"):
                raise OzonError("Этикетки: " + ", ".join(r["errors"]["error_reasons"]))
            it["op"] = r.get("operation_id")
            it["ids"] = ids
            it["stage"] = "label_wait"
            return

        if st == "label_wait_cargo":
            for key, op in list(it["cops"].items()):
                pdf = self._label_ready(op, too_long)
                if pdf is None:
                    continue
                cid = int(s["cargo_ids"][key])
                rel = self.single_label(s, key, cid, pdf)
                s["cargo_pdf"][key] = rel
                it["cops"].pop(key)
                self.save()
            if not it["cops"]:
                self.assemble_from_cargo(s)
                self._labels_done(s)
                it["stage"] = "pass"
            return

        if st == "label_wait_groups":
            if "gops" not in it:
                it["stage"] = "label"
                return
            for gid, op in list(it["gops"].items()):
                pdf = self._label_ready(op, too_long)
                if pdf is None:
                    continue
                rel = f"raw/{s['supply_id']}_{gid}.pdf"
                with open(os.path.join(self.dir, rel), "wb") as f:
                    f.write(pdf)
                s["labels"][gid] = rel
                it["gops"].pop(gid)
                self.save()
            if not it["gops"]:
                self._labels_done(s)
                it["stage"] = "pass"
            return

        if st == "label_wait":
            pdf = self._label_ready(it["op"], too_long)
            if pdf is None:
                return
            if not self.split_labels(s, pdf, it["ids"]):
                it["stage"] = "label"           # по одному грузоместу (labels_per_cargo)
                return
            self._labels_done(s)
            it["stage"] = "pass"
            return

        if st == "pass":
            if not self.batch.get("multi"):
                self.set(s, "pass")
                self.make_pass(s["order_id"], s, s)
            self.set(s, "done")
            it["stage"] = "done"

    def verify_contents(self, s, it):
        """Проверка №1: спрашиваем у Озона, что записано в каждом грузоместе, и сверяем с расчётом.

        Возвращает True, когда проверка закончена (или недоступна), False — если Озон ещё не готов.
        Если хоть в одном грузоместе не то — ошибка, этикетки для поставки не выпускаются."""
        names = {p["sku"]: (p.get("short") or p.get("name") or p["sku"]) for p in self.batch["products"]}
        ids = {str(b["key"]): int(s["cargo_ids"][str(b["key"])]) for b in s["boxes"]}
        try:
            r = self.api.post("/v2/cargoes/get", {"supplies": [
                {"supply_id": int(s["supply_id"]), "cargo_ids": [str(i) for i in ids.values()]}]})
            bundles = {}
            for sup in r.get("supplies") or []:
                for c in sup.get("cargoes") or []:
                    if c.get("cargo_id") and c.get("bundle_id"):
                        bundles[int(c["cargo_id"])] = c["bundle_id"]
            if len(bundles) < len(ids):
                if time.time() - it.setdefault("verify_t", time.time()) < 300:
                    return False
                raise OzonError("Озон не показывает состав части грузомест")
            bad = []
            for b in s["boxes"]:
                cid = ids[str(b["key"])]
                rb = self.api.post("/v1/supply-order/bundle", {"bundle_ids": [bundles[cid]], "limit": 100})
                items = [x for x in rb.get("items") or [] if int(x.get("quantity") or 0) > 0]
                if not items:
                    if time.time() - it.setdefault("verify_t", time.time()) < 300:
                        return False
                    raise OzonError("Озон не показывает состав части грузомест")
                got = {str(x.get("sku")): int(x.get("quantity") or 0) for x in items}
                if got != {str(b["sku"]): int(b["qty"])}:
                    have = ", ".join(f"{names.get(k, 'SKU ' + k)} × {v}" for k, v in got.items())
                    bad.append(f"грузоместо {cid}: ожидалось {names.get(b['sku'], b['sku'])} × {b['qty']}, у Озона {have}")
            if bad:
                raise OzonError("Состав грузомест у Озона не совпадает с расчётом — этикетки для этой поставки "
                                "не выпущены, проверьте поставку в кабинете: " + "; ".join(bad))
            s["verified"] = len(ids)
            self.log(f"Состав грузомест подтверждён Озоном: {len(ids)} из {len(ids)}", s)
        except OzonError as e:
            if "не совпадает" in str(e):
                raise
            # сама проверка недоступна (метод/права) — не блокируем, этикетки всё равно раскладываются по номерам
            s["verified"] = "unavailable"
            s.setdefault("warn", []).append(f"Сверить состав грузомест с Озоном не удалось: {e}")
            self.log(f"Сверить состав грузомест с Озоном не удалось: {e}", s, "warn")
        self.save()
        return True

    def _label_ready(self, op, too_long):
        """PDF этикеток по операции или None, если Озон ещё готовит."""
        g = self.api.post("/v1/cargoes-label/get", {"operation_id": op})
        status = g.get("status")
        if status in ("IN_PROGRESS", None, ""):
            if too_long:
                raise OzonError("Озон долго готовит этикетки — нажмите «Продолжить» позже")
            return None
        res = g.get("result") or {}
        if status != "SUCCESS" or not (res.get("file_url") or res.get("file_guid")):
            raise OzonError("Этикетки не сформированы: " + ", ".join(
                (g.get("errors") or {}).get("error_reasons") or [status or ""]))
        url = res.get("file_url") or f"https://api-seller.ozon.ru/v1/cargoes-label/file/{res['file_guid']}"
        pdf = self.api.get_file(url)
        if not pdf.startswith(b"%PDF"):
            raise OzonError("Озон вернул не PDF-файл этикеток")
        return pdf

    def single_label(self, s, key, cid, pdf):
        """Файл этикетки одного грузоместа. В нём должна быть этикетка именно этого грузоместа."""
        import io
        from pypdf import PdfReader, PdfWriter
        pages = list(PdfReader(io.BytesIO(pdf)).pages)
        all_ids = [int(x) for x in s["cargo_ids"].values()]
        own = [p for p in pages if L.cargo_ids_on_page(p, all_ids) == [cid]]
        alien = [p for p in pages if any(c != cid for c in L.cargo_ids_on_page(p, all_ids))]
        if alien:
            raise OzonError(f"Озон прислал для грузоместа {cid} этикетку другого грузоместа — нажмите «Продолжить»")
        if own:
            chosen = own[0]
        elif pages and len({L.page_hash(p) for p in pages}) == 1:
            chosen = pages[0]               # номер не читается, но все страницы одинаковые
        else:
            raise OzonError(f"Не удалось опознать этикетку грузоместа {cid} ({len(pages)} стр.)")
        w = PdfWriter()
        w.add_page(chosen)
        os.makedirs(os.path.join(self.dir, "raw"), exist_ok=True)
        rel = f"raw/{s['supply_id']}_c{cid}.pdf"
        with open(os.path.join(self.dir, rel), "wb") as f:
            w.write(f)
        return rel

    def assemble_from_cargo(self, s):
        """Файлы составов поставки из этикеток отдельных грузомест (по одной странице на коробку)."""
        from pypdf import PdfReader, PdfWriter
        for gid, boxes in self.groups_of(s).items():
            w = PdfWriter()
            for b in boxes:
                w.add_page(PdfReader(os.path.join(self.dir, s["cargo_pdf"][str(b["key"])])).pages[0])
            rel = f"raw/{s['supply_id']}_{gid}.pdf"
            with open(os.path.join(self.dir, rel), "wb") as f:
                w.write(f)
            s["labels"][gid] = rel
        self.save()

    def _labels_done(self, s):
        self.log("Этикетки получены", s)
        try:
            self.rebuild_outputs()            # файлы составов обновляются сразу
        except Exception:
            self._api_log(traceback.format_exc())

    @staticmethod
    def groups_of(s):
        out = {}
        for b in s["boxes"]:
            out.setdefault(f"{b['sku']}x{b['qty']}", []).append(b)
        return out

    def split_labels(self, s, pdf, ids):
        """Раскладывает страницы общего PDF поставки по составам коробок.

        Страницу узнаём по номеру грузоместа, напечатанному на этикетке. Если хоть один номер
        не опознан — возвращаем False, и этикетки запрашиваются отдельно по составам."""
        import io
        from pypdf import PdfReader, PdfWriter
        raw = os.path.join(self.dir, "raw")
        os.makedirs(raw, exist_ok=True)
        with open(os.path.join(raw, f"{s['supply_id']}_all.pdf"), "wb") as f:
            f.write(pdf)
        reader = PdfReader(io.BytesIO(pdf))
        pages = list(reader.pages)
        by_cargo, problems = {}, []
        if len(set(ids)) != len(ids):
            problems.append("у двух коробок один номер грузоместа")
        for i, page in enumerate(pages):
            hits = L.cargo_ids_on_page(page, ids)
            if len(hits) > 1:
                problems.append(f"на странице {i + 1} несколько номеров: {', '.join(map(str, hits))}")
            elif len(hits) == 1:
                c = hits[0]
                if c in by_cargo and L.page_hash(pages[by_cargo[c]]) != L.page_hash(page):
                    problems.append(f"грузоместо {c} — на двух разных страницах")
                by_cargo.setdefault(c, i)
        missing = [c for c in ids if c not in by_cargo]
        if missing:
            problems.append(f"нет этикеток для {len(missing)} грузомест")
        if problems:
            # раскладывать «на глаз» не рискуем — запросим этикетки по одному грузоместу
            s["labels_per_cargo"] = True
            self._api_log(f"{s['name']}: раскладка общего PDF не прошла проверку ({'; '.join(problems)}) — "
                          f"запрашиваю этикетки по одному грузоместу")
            self.log("Этикетки поставки не прошли проверку — перезапрашиваю по одной на грузоместо", s, "warn")
            return False
        key_to_id = {str(b["key"]): int(s["cargo_ids"][str(b["key"])]) for b in s["boxes"]}
        # проверка №3: если на этикетке напечатан товар (артикул или SKU) — он должен быть свой
        prods = {p["sku"]: {t for t in (str(p["sku"]), str(p.get("offer_id") or "")) if len(t) >= 4}
                 for p in self.batch["products"]}

        def has(text, token):
            return re.search(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", text) is not None

        texts = []
        for page in pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        for b in s["boxes"]:
            t = texts[by_cargo[key_to_id[str(b["key"])]]]
            own = any(has(t, x) for x in prods.get(b["sku"], ()))
            alien = [sku for sku, toks in prods.items() if sku != b["sku"] and any(has(t, x) for x in toks)]
            if alien and not own:
                raise OzonError(f"На этикетке грузоместа {key_to_id[str(b['key'])]} напечатан другой товар "
                                f"(SKU {', '.join(alien)}) — этикетки для этой поставки не выпущены")
        for gid, boxes in self.groups_of(s).items():
            w = PdfWriter()
            for b in boxes:
                w.add_page(pages[by_cargo[key_to_id[str(b["key"])]]])
            rel = f"raw/{s['supply_id']}_{gid}.pdf"
            with open(os.path.join(self.dir, rel), "wb") as f:
                w.write(f)
            s["labels"][gid] = rel
        self.save()
        return True

    def ship_error(self, s, e):
        if self.stop.is_set():
            raise OzonError("Остановлено пользователем")
        self.failed_now.add(skey(s))
        self.set(s, "error", str(e))
        self.log(str(e), s, "error")

    def cargo_phase(self, ships):
        """Догоняет все поставки, у которых уже есть заявка (при «Продолжить» и после создания)."""
        for s in ships:
            if s.get("order_id") and s["status"] != "done" and skey(s) not in self.failed_now \
                    and not any(it["s"] is s for it in self.pipe):
                self.pipe_add(s)
        self.pipe_drain()

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
        b.pop("interrupted", None)
        self.recover_submits()                   # заявки, отправленные перед остановкой программы
        self.take_replacements()                 # замены, выбранные пока программа не работала
        if b.get("multi"):
            return self.run_multi()
        ships = self.active()

        auto = b.get("mode") in ("auto", "none")
        # 1. Черновики — только для ручного выбора: нужен список слотов всех городов сразу.
        #    В автоматическом режиме каждый город проходит путь целиком (черновик → слоты → бронь),
        #    и только потом следующий: пока создаются чужие черновики, ближние слоты не успевают уйти.
        if not b.get("chosen_slot") and not auto:
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
        for s in ships:                      # уже созданные (при «Продолжить») — сразу в конвейер
            if s.get("order_id"):
                self.pipe_add(s)
        later = []                           # города без слотов — спросим ещё раз в конце
        for s in ships:
            if s.get("order_id") or skey(s) in self.failed_now or s["status"] == "done":
                continue
            if later:
                self.retry_due(later)        # пока идут черновики — проверяем, не появились ли места
            if auto and not self.draft_alive(s):
                self.idle_until("/v1/draft/crossdock/create")
            self.idle_until("/v2/draft/supply/create")     # пока ждём лимит — двигаем конвейер
            if self.no_slot_mode():
                self.safe(s, self.create_supply_noslot)
            else:
                self.safe(s, self.create_supply, later)
            if s.get("order_id"):
                self.pipe_add(s)
        self.retry_no_slots(later)

        self.take_replacements()

        # 3. Догоняем грузоместа, этикетки, пропуска
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
                self.fail(self.no_slots_text())
                return False
            # для общей заявки слот один; для отдельных — запоминаем самый частый для отображения
            b["chosen_slot"] = max((p for p in plan if p), key=lambda p: (sum(1 for q in plan if q == p), -S.parse(p[0]).timestamp()))
            b["chosen_slot_human"] = S.summary(plan)
            inside = sum(1 for p in plan if p and S.day_shift(p, pref) == 0)
            if len(slot_lists) == 1:
                self.log(f"Выбран таймслот {S.human(plan[0])}" + ("" if inside else " (в желаемый день свободных нет — ближайший)"),
                         lvl="info" if inside else "warn")
            else:
                self.log(f"Таймслоты по городам: в желаемый день — {inside} из {len(plan)}, остальным ближайший день. "
                         f"{S.summary(plan)}", lvl="info" if inside == len(plan) else "warn")
            self.save()
            return True
        self.take_replacements()                 # без таймслота — только применяем замену
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
        try:
            chk = L.verify_outputs(b, self.dir)
            b["labels_check"] = chk
            self.log(("Проверка этикеток: " if chk["ok"] else "Проверка этикеток не пройдена: ") + chk["summary"],
                     lvl="info" if chk["ok"] else "error")
        except Exception as e:
            self._api_log(traceback.format_exc())
            b["labels_check"] = {"ok": False, "summary": f"проверить не удалось: {e}", "problems": []}
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
                if not b.get("chosen_slot") and not self.no_slot_mode():
                    if not self.pick_slot([M["slots"]], M["draft_at"]):
                        return
            elif time.time() - M.get("slots_at", 0) > 90:
                M["slots"] = self.fetch_slots(M["draft_id"], "MULTI_CLUSTER", self.multi_ships())
                M["slots_at"] = time.time()

            b["phase"] = "supplies"
            self.save()
            live = self.multi_ships()
            tried = []
            for s in live:
                self.set(s, "supply")
            for attempt in range(4):
                if self.no_slot_mode():
                    slot = None
                    try:
                        M["order_id"] = self.submit_supply(M["draft_id"], "MULTI_CLUSTER", live, None, M)
                    except OzonError as e:
                        msg = f"Заявку без таймслота Озон не принял: {e}"
                        for s in live:
                            self.set(s, "error", msg)
                        return self.fail(msg)
                    break
                avail = [x for x in M["slots"] if S.key(x) not in {S.key(t) for t in tried}]
                slot = (S.choose_for_city(avail, b["slot_pref"]) if b.get("mode") == "auto" else self.take_slot(avail, M))
                if not slot:
                    for s in live:
                        self.set(s, "error", self.no_slots_text())
                    return self.fail(self.no_slots_text())
                try:
                    M["order_id"] = self.submit_supply(M["draft_id"], "MULTI_CLUSTER", live, slot, M)
                    break
                except OzonError as e:
                    if "TIMESLOT_NOT_AVAILABLE" in getattr(e, "codes", ()) and attempt < 3:
                        tried.append(slot)
                        self.log(f"Таймслот {S.human(slot)} успели занять — беру следующий свободный", lvl="warn")
                        M["slots"] = self.fetch_slots(M["draft_id"], "MULTI_CLUSTER", live)
                        continue
                    for s in live:
                        self.set(s, "error", str(e))
                    return self.fail(str(e))
            M["slot"] = slot
            b["slot_locked"] = True
            for s in live:
                s["order_id"] = M["order_id"]
                s["slot"] = slot
                s["no_slot"] = not slot
            if slot:
                self.log(f"Заявка создана, id {M['order_id']}, {len(live)} городов, таймслот {S.human(slot)}")
            else:
                b["chosen_slot_human"] = "без таймслота"
                self.log(f"Заявка создана, id {M['order_id']}, {len(live)} городов, без таймслота — "
                         f"выберите таймслот в кабинете Озона", lvl="warn")

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
        for s in self.active():                  # заменённые кластеры — отдельными заявками
            if not s.get("order_id") and s["status"] == "new" and not s.get("excluded"):
                self.safe(s, self.create_supply)
                if s.get("order_id"):
                    live.append(s)
        self.take_replacements()
        self.cargo_phase(live)
        if any(s["status"] == "done" for s in live):
            self.make_pass(M["order_id"], M)
        self.finish()

    def multi_ships(self):
        did = (self.batch.get("order") or {}).get("draft_id")
        return [s for s in self.active() if not s.get("excluded") and s.get("draft_id") == did]

    def create_multi_draft(self, M):
        """Черновик на несколько кластеров. Кластеры, которые Озон не принимает, исключаются."""
        ships, seen = [], set()
        for s in self.active():
            if s.get("excluded") or s.get("imported") or str(s["cluster_id"]) in seen:
                continue                      # второй раз тот же кластер — пойдёт отдельной заявкой
            seen.add(str(s["cluster_id"]))
            ships.append(s)
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
    def retry_due(self, later):
        """Повторный запрос слотов у отложенных городов, чья минута уже прошла (без ожидания)."""
        for s in list(later):
            if s.get("cancelled") or s.get("order_id") or skey(s) in self.failed_now:
                later.remove(s)
                continue
            if time.time() < s["no_slots_at"] + RETRY_NO_SLOTS:
                continue
            later.remove(s)
            s["slots_at"] = 0
            self.safe(s, self.create_supply, later, quiet=True)
            if s.get("order_id"):
                self.pipe_add(s)
                for x in later:              # места появились — остальных спрашиваем сразу
                    x["no_slots_at"] = 0

    def retry_no_slots(self, later):
        """Города без слотов: спрашиваем раз в минуту, пока слоты не появятся (до WAIT_NO_SLOTS).
        Как только слоты получил хоть один город — остальных спрашиваем сразу."""
        if not later:
            return
        deadline = time.time() + WAIT_NO_SLOTS
        self.log(f"Без таймслотов: {len(later)} из {len(self.active())}. Жду, пока у точки появятся места, — "
                 f"спрашиваю Озон раз в минуту, до {WAIT_NO_SLOTS // 60} мин; не появятся — создам заявки "
                 f"без таймслота", lvl="warn")
        pending = list(later)
        while pending:
            pending = [s for s in pending if not s.get("cancelled") and not s.get("order_id")]
            if not pending:
                break
            s = min(pending, key=lambda x: x["no_slots_at"])
            while time.time() < s["no_slots_at"] + RETRY_NO_SLOTS:   # ждём, двигая конвейер
                self.check_stop()
                self.pipe_tick()
                self.api.sleep(min(2.0, max(0.1, s["no_slots_at"] + RETRY_NO_SLOTS - time.time())))
            s["slots_at"] = 0                # слоты запросим заново
            if time.time() >= deadline:      # слоты так и не появились — заявка без таймслота
                pending.remove(s)
                self.safe(s, self.create_supply_noslot)
                if s.get("order_id"):
                    self.pipe_add(s)
                continue
            again = []
            self.safe(s, self.create_supply, again, quiet=True)
            if s.get("order_id"):
                self.pipe_add(s)
                for x in pending:            # места появились — остальных спрашиваем сразу
                    x["no_slots_at"] = 0
            elif not again:
                pending.remove(s)            # ошибка — город больше не ждём

    def safe(self, s, fn, later=None, quiet=False):
        try:
            fn(s)
        except NoSlots as e:
            if later is None or self.stop.is_set():
                self.failed_now.add(skey(s))
                self.set(s, "error", str(e))
                self.log(str(e), s, "error")
                return
            s["no_slots_at"] = time.time()
            later.append(s)
            self.set(s, "draft_ok", "нет таймслотов — жду, пока появятся")
            if not quiet:
                self.log(str(e) + ". Повторю позже, после остальных городов", s, "warn")
        except OzonError as e:
            if self.stop.is_set():
                self.set(s, s["status"], "остановлено")
                raise
            self.failed_now.add(skey(s))
            self.set(s, "error", str(e))
            self.log(str(e), s, "error")
        except Exception as e:  # неожиданная ошибка — пишем подробно в api.log
            self._api_log(traceback.format_exc())
            self.failed_now.add(skey(s))
            self.set(s, "error", f"Внутренняя ошибка: {e}")
            self.log(f"Внутренняя ошибка: {e}", s, "error")

    def rebuild_outputs(self):
        b = self.batch
        act = self.active()
        names = {skey(s): s["name"] for s in act}
        b["groups"] = P.group_boxes({skey(s): s["boxes"] for s in act}, b["products"],
                                    [skey(s) for s in act], names)
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
                r.batch["error"] = SHUTDOWN_MSG if SHUTTING_DOWN.is_set() else str(e)
                r.log(r.batch["error"], lvl="warn" if stop.is_set() else "error")
        except Exception as e:
            if r:
                r._api_log(traceback.format_exc())
                r.batch["phase"] = "error"
                r.batch["error"] = f"Внутренняя ошибка: {e}"
                r.save()
        finally:
            if r:
                r._apilog.close()
            if (REPLACE.get(bid) or ADD.get(bid) or MERGE.get(bid)) and not stop.is_set():
                # замену выбрали, когда работа уже заканчивалась — запускаем ещё раз
                threading.Timer(0.3, lambda: start(pid, bid)).start()

    t = threading.Thread(target=target, daemon=True)
    RUNNING[bid] = (t, stop)
    t.start()
    return True


def stop(bid):
    if bid in RUNNING:
        RUNNING[bid][1].set()


SHUTTING_DOWN = threading.Event()
SHUTDOWN_MSG = "Программа перезапускалась (обновление) — нажмите «Продолжить»"
INTERRUPTED_MSG = "Программа была остановлена, пока создавались поставки — нажмите «Продолжить»"


def shutdown(timeout=50):
    """Остановка программы (Docker Up, закрытие): просим все партии остановиться и ждём, пока текущие
    шаги доделаются — заявка, уже отправленная в Озон, дожидается своего номера. Возвращает, сколько
    потоков не успели остановиться."""
    SHUTTING_DOWN.set()
    busy = [(bid, t, ev) for bid, (t, ev) in list(RUNNING.items()) if t.is_alive()]
    for _, t, ev in busy:
        ev.set()                         # отмена заявок флаг не смотрит — её просто ждём
    deadline = time.time() + timeout
    for _, t, _ in busy:
        t.join(max(0.0, deadline - time.time()))
    return sum(1 for _, t, _ in busy if t.is_alive())


def mark_interrupted():
    """При запуске: партии, которые создавались или отменялись, когда программу остановили, —
    иначе они навсегда «создаются» без кнопки «Продолжить»."""
    n = 0
    for p in ST.load_profiles()["profiles"]:
        for b in ST.list_batches(p["id"]):
            ph = b.get("phase")
            if ph in ("drafts", "supplies", "cargo"):
                b["phase"], b["error"], b["interrupted"] = "stopped", INTERRUPTED_MSG, True
                msg = INTERRUPTED_MSG
            elif ph == "cancelling":
                b["phase"] = "partial"
                b["error"] = msg = ("Отмена заявок прервалась: программа была остановлена. "
                                    "Нажмите «Сверить с Озоном» и при необходимости отмените ещё раз")
            else:
                continue
            b.setdefault("log", []).append({"t": time.strftime("%H:%M:%S"), "city": "", "msg": msg, "lvl": "warn"})
            ST.save_batch(p["id"], b)
            n += 1
    return n


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


# ------------------------------------------------------------------ добавить поставку по номеру
ACTIVE_STATES = ["DATA_FILLING", "READY_TO_SUPPLY"]


def _digits(x):
    return re.sub(r"\D", "", str(x or ""))


def _list_order_ids(api, limit=1000):
    """ID заявок магазина, которые ещё не отгружены (заполнение данных / готова к поставке)."""
    variants = [
        {"filter": {"states": ACTIVE_STATES}, "limit": 100, "sort_by": "ORDER_CREATION", "sort_dir": "DESC"},
        {"filter": {"states": ACTIVE_STATES}, "limit": 100},
    ]
    err = None
    for body in variants:
        out, last = [], None
        try:
            while len(out) < limit:
                r = api.post("/v3/supply-order/list", dict(body, last_id=last) if last else body)
                ids = r.get("order_ids") or r.get("supply_order_id") or []
                out += [int(x) for x in ids]
                last = r.get("last_id")
                if not ids or not last:
                    break
            return out
        except OzonError as e:
            err = e
            if e.status not in (400, 404):
                break
    raise OzonError(f"Не удалось получить список заявок у Озона: {err}")


def find_orders(api, number):
    """Номер из кабинета/с этикетки (номер заявки), ID заявки или номер поставки → [(заявка, поставка|None)]."""
    n = _digits(number)
    if not n:
        raise OzonError(f"«{number}» — не номер")
    try:
        r = api.post("/v3/supply-order/get", {"order_ids": [int(n)]})
        o = next((x for x in r.get("orders") or [] if str(x.get("order_id")) == n), None)
        if o:
            return o, None
    except OzonError:
        pass                                  # не ID заявки — ищем по номеру среди заявок магазина
    ids = _list_order_ids(api)
    for i in range(0, len(ids), 50):
        r = api.post("/v3/supply-order/get", {"order_ids": ids[i:i + 50]})
        for o in r.get("orders") or []:
            if _digits(o.get("order_number")) == n:
                return o, None
            for sup in o.get("supplies") or []:
                if str(sup.get("supply_id")) == n:
                    return o, sup
    raise OzonError(f"Заявка {number} не найдена у Озона (ищу среди неотгруженных заявок этого магазина)")


def _bundle_items(api, bundle_id):
    items, last = [], None
    for _ in range(50):
        body = {"bundle_ids": [bundle_id], "limit": 100}
        if last:
            body["last_id"] = last
        r = api.post("/v1/supply-order/bundle", body)
        items += [x for x in r.get("items") or [] if int(x.get("quantity") or 0) > 0]
        last = r.get("last_id")
        if not r.get("has_next") or not last:
            break
    return items


def read_supply(api, order, sup, names, products):
    """Поставка из Озона → поставка партии: кластер, состав, грузоместа (если уже созданы в Озоне)."""
    sid = int(sup["supply_id"])
    ml = str(sup.get("macrolocal_cluster_id") or "")
    name = names.get(ml) or ((sup.get("storage_warehouse") or {}).get("name")) or f"Поставка {sid}"
    info = {}                                         # sku → {offer_id, name}
    r = api.post("/v2/cargoes/get", {"supplies": [{"supply_id": sid}]})
    cargoes = [c for x in r.get("supplies") or [] if int(x.get("supply_id") or sid) == sid
               for c in x.get("cargoes") or [] if c.get("cargo_id")]
    boxes, cargo_ids, items = [], {}, {}
    for n, c in enumerate(cargoes, 1):
        if not c.get("bundle_id"):
            raise OzonError(f"Озон не показывает состав грузоместа {c['cargo_id']}")
        its = _bundle_items(api, c["bundle_id"])
        if len({str(x.get("sku")) for x in its}) != 1:
            raise OzonError(f"Грузоместо {c['cargo_id']}: " + ("пустое" if not its else "в нём несколько товаров") +
                            " — программа работает только с монокоробами")
        sku, qty = str(its[0]["sku"]), sum(int(x.get("quantity") or 0) for x in its)
        info.setdefault(sku, {"offer_id": its[0].get("offer_id"), "name": its[0].get("name")})
        key = f"i{n:03d}-{sku}-{qty}"
        boxes.append({"key": key, "sku": sku, "qty": qty})
        cargo_ids[key] = int(c["cargo_id"])
        items[sku] = items.get(sku, 0) + qty
    if not cargoes:                                   # грузомест ещё нет — создаст программа
        if not sup.get("bundle_id"):
            raise OzonError(f"Поставка {sid}: Озон не показывает её состав")
        for x in _bundle_items(api, sup["bundle_id"]):
            sku = str(x.get("sku"))
            info.setdefault(sku, {"offer_id": x.get("offer_id"), "name": x.get("name")})
            items[sku] = items.get(sku, 0) + int(x.get("quantity") or 0)
        if not items:
            raise OzonError(f"Поставка {sid}: в ней нет товаров")
        prods = {p["sku"]: p for p in products}
        plist = [dict(prods.get(s) or {"sku": s}, sku=s) for s in items]
        boxes = P.build_boxes({"x": items}, plist, ["x"])["x"]
    s = {"cluster_id": ml or f"s{sid}", "cluster_name": name, "name": name, "items": items, "boxes": boxes,
         "order_id": order.get("order_id"), "order_number": order.get("order_number"),
         "order_state": order.get("state"), "supply_id": sid,
         "storage_name": (sup.get("storage_warehouse") or {}).get("name"),
         "status": "supply", "error": None, "imported": True, "pass_done": "imported",
         "warn": [f"Добавлена по номеру {order.get('order_number') or order.get('order_id')}"
                  + ("" if cargoes else " — грузоместа создаст программа")]}
    if cargoes:
        s["cargo_ids"] = cargo_ids
        s["verified"] = len(cargoes)                  # состав взят у самого Озона
    return s, info


def fetch_import(pid, bid, number, batch):
    """Находит заявку у Озона и готовит её поставки для партии. Возвращает (поставки, сведения о товарах)."""
    api, lf = _batch_api(pid, bid)
    try:
        order, only = find_orders(api, number)
        if order.get("state") == "CANCELLED":
            raise OzonError(f"Заявка {number} отменена")
        sups = [only] if only else [x for x in order.get("supplies") or [] if x.get("supply_id")]
        if not sups:
            raise OzonError(f"В заявке {number} пока нет поставок — Озон ещё не выдал их номера, попробуйте через минуту")
        have = {str(s.get("supply_id")) for s in batch["shipments"] if s.get("supply_id") and not s.get("cancelled")}
        names = cluster_names(api)
        ships, info, skipped = [], {}, []
        for sup in sups:
            if str(sup["supply_id"]) in have:
                skipped.append(str(sup["supply_id"]))
                continue
            s, inf = read_supply(api, order, sup, names, batch["products"])
            ships.append(s)
            for k, v in inf.items():
                info.setdefault(k, v)
        if not ships:
            raise OzonError(f"Заявка {number} уже есть в этой партии")
        return ships, info
    finally:
        lf.close()


def merge_import(batch, ships, prods):
    """Добавляет поставки в партию: уникальные ключи, новые товары, запись в журнал. Возвращает добавленные."""
    have = {str(s.get("supply_id")) for s in batch["shipments"] if s.get("supply_id") and not s.get("cancelled")}
    taken = {skey(x) for x in batch["shipments"]}
    by_sku = {p["sku"]: p for p in batch["products"]}
    out = []
    for s in ships:
        if str(s["supply_id"]) in have:
            continue
        key, n = str(s["cluster_id"]), 2
        while key in taken:
            key, n = f"{s['cluster_id']}#{n}", n + 1
        s["key"] = key
        taken.add(key)
        have.add(str(s["supply_id"]))
        for sku, q in s["items"].items():
            p = by_sku.get(sku)
            if p is None:
                p = dict(prods.get(sku) or {}, sku=sku, qty=0)
                p.setdefault("name", sku)
                p.setdefault("short", "")
                p.setdefault("box_size", None)
                batch["products"].append(p)
                by_sku[sku] = p
            p["qty"] = int(p.get("qty") or 0) + int(q)
        batch["shipments"].append(s)
        out.append(s)
        units = sum(s["items"].values())
        batch.setdefault("log", []).append({
            "t": time.strftime("%H:%M:%S"), "city": s["name"], "lvl": "info",
            "msg": f"Добавлена поставка {s['supply_id']} (заявка {s.get('order_number') or s.get('order_id')}): "
                   f"{units} шт, {len(s['boxes'])} грузомест" + ("" if s.get("cargo_ids") else " — создам в Озоне")})
    return out


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
                   and (not cluster_ids or skey(s) in cluster_ids)]
        oids = {str(s["order_id"]) for s in targets if s.get("order_id")}
        targets += [s for s in batch["shipments"] if not s.get("cancelled") and s not in targets
                    and s.get("order_id") and str(s["order_id"]) in oids]   # заявка на несколько городов
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
