"""Хранение настроек магазинов и партий поставок.

Программа (.exe) хранит данные в постоянной папке пользователя: %APPDATA%\\Поставки Ozon\\data.
Она не зависит от того, где лежит .exe, поэтому замена или перенос программы ничего не стирают.
"""
import copy
import json
import os
import shutil
import sys
import threading
import time
import uuid

APP_DIR_NAME = "Поставки Ozon"


def _data_root():
    if os.environ.get("OZON_SUPPLIES_DATA"):
        return os.environ["OZON_SUPPLIES_DATA"]
    if getattr(sys, "frozen", False) or sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_DIR_NAME, "data")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


DATA = _data_root()
IMPORT_NOTE = os.path.join(DATA, "imported_from.txt")


def copy_file(src, dst):
    """Копирование без системных CopyFile2/CopyFileEx — работает в любой Windows и в Wine."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    return dst


def copy_tree(src, dst):
    for cur, dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for f in files:
            copy_file(os.path.join(cur, f), os.path.join(target, f))


# ---------------- перенос данных из старых версий ----------------
def _has_key(profiles_path):
    """Есть ли в файле настроек хотя бы один магазин с API-ключом."""
    try:
        with open(profiles_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return any(p.get("api_key") for p in d.get("profiles") or [])
    except Exception:
        return False


def _legacy_roots():
    """Где могли остаться данные прошлых версий (папка data рядом с .exe)."""
    home = os.path.expanduser("~")
    roots = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        roots += [exe_dir, os.path.dirname(exe_dir)]
    for sub in ("Desktop", "Рабочий стол", "Downloads", "Загрузки", "Documents", "Документы",
                "OneDrive", os.path.join("OneDrive", "Desktop"), os.path.join("OneDrive", "Рабочий стол"),
                os.path.join("OneDrive", "Documents"), os.path.join("OneDrive", "Документы")):
        roots.append(os.path.join(home, sub))
    for env in ("TEMP", "TMP"):
        if os.environ.get(env):
            roots.append(os.environ[env])
    if os.environ.get("LOCALAPPDATA"):
        roots.append(os.path.join(os.environ["LOCALAPPDATA"], APP_DIR_NAME))
    for drive in ("C:\\", "D:\\"):
        roots.append(drive)
    out = []
    for r in roots:
        r = os.path.abspath(r)
        if os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def find_legacy(max_depth=4, time_limit=8.0):
    """Ищет папки data со старыми настройками. Возвращает [(путь к data, время изменения)], свежие первыми."""
    t0 = time.time()
    found = {}
    skip = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "appdata",
            "node_modules", ".git", "system volume information"}
    own = os.path.abspath(DATA)
    for root in _legacy_roots():
        base_depth = root.rstrip("\\/").count(os.sep)
        for cur, dirs, files in os.walk(root):
            if time.time() - t0 > time_limit:
                break
            depth = cur.rstrip("\\/").count(os.sep) - base_depth
            dirs[:] = [d for d in dirs if d.lower() not in skip and not d.startswith(".")] if depth < max_depth else []
            if os.path.basename(cur).lower() == "data" and "profiles.json" in files:
                p = os.path.abspath(cur)
                if p != own and _has_key(os.path.join(p, "profiles.json")):
                    found[p] = os.path.getmtime(os.path.join(p, "profiles.json"))
                dirs[:] = []
    return sorted(found.items(), key=lambda x: -x[1])


def migrate_legacy():
    """Если в постоянной папке ещё нет магазина с ключом — переносит самые свежие старые данные."""
    if os.environ.get("OZON_SUPPLIES_DATA"):
        return None
    if _has_key(os.path.join(DATA, "profiles.json")):
        return None
    flag = os.path.join(DATA, ".legacy-scanned")
    if os.path.exists(flag):
        return None
    try:
        cands = find_legacy()
    except Exception:
        cands = []
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(flag, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M"))
    except OSError:
        pass
    if not cands:
        return None
    return import_dir(cands[0][0])


def import_dir(src):
    """Копирует папку data (настройки и партии) в постоянную папку программы."""
    os.makedirs(DATA, exist_ok=True)
    cur = os.path.join(DATA, "profiles.json")
    if os.path.exists(cur):
        copy_file(cur, os.path.join(DATA, "profiles.before-import.json"))
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(DATA, name)
        if os.path.isdir(s):
            copy_tree(s, d)
        elif name != "program.log":
            copy_file(s, d)
    with open(IMPORT_NOTE, "w", encoding="utf-8") as f:
        f.write(src)
    return src


def take_import_note():
    """Однократное сообщение «настройки перенесены из …» для интерфейса."""
    try:
        with open(IMPORT_NOTE, "r", encoding="utf-8") as f:
            src = f.read().strip()
        os.replace(IMPORT_NOTE, IMPORT_NOTE + ".shown")
        return src
    except OSError:
        return None


_lock = threading.RLock()

# Города по умолчанию: название в программе, ключевое слово для поиска кластера Озона, доля %
DEFAULT_CITIES = [
    ("Москва", "москва", 25), ("Санкт-Петербург", "петербург", 10), ("Ростов-на-Дону", "ростов", 10),
    ("Тверь", "тверь", 5), ("Ярославль", "ярославль", 5), ("Екатеринбург", "екатеринбург", 5),
    ("Пермь", "пермь", 5), ("Оренбург", "оренбург", 5), ("Казань", "казань", 5),
    ("Воронеж", "воронеж", 5), ("Краснодар", "краснодар", 5), ("Уфа", "уфа", 5),
    ("Саратов", "саратов", 5), ("Невинномысск", "невинномысск", 5),
]

DEFAULT_SETTINGS = {
    "name": "Мой магазин",
    "clusters_v": 2,          # версия привязки городов к кластерам
    "client_id": "",
    "api_key": "",
    "demo": False,
    "dropoff": None,          # точка по умолчанию {warehouse_id, name, warehouse_type, address}
    "dropoffs": [],           # избранные точки отгрузки (выбор в один клик)
    "dropoff_seeds": ["Кавказский", "Строгино"],   # найти в Озоне и добавить в избранные
    "pass": {"enabled": False, "driver_name": "", "driver_phone": "", "vehicle_model": "", "vehicle_number": ""},
    "products": {},           # sku -> {name, short, offer_id, box_size, multiple}
    "templates": [{
        "name": "Основной",
        "cities": [{"cluster_id": None, "name": n, "match": m, "share": s} for n, m, s in DEFAULT_CITIES],
    }],
    "template": "Основной",
    "method": "remainder_city",
    "remainder_city": "largest",
    "timeslot": {"mode": "manual", "days_from": 1, "days_to": 7, "time_from": "09:00", "time_to": "18:00"},
}


def _path(*p):
    return os.path.join(DATA, *p)


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(default)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _merge_defaults(s):
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for k, v in (s or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("products",):
            out[k].update(v)
        else:
            out[k] = v
    return out


# ---------------- профили (магазины) ----------------
def load_profiles():
    with _lock:
        d = _read(_path("profiles.json"), {"active": None, "profiles": []})
        if not d["profiles"]:
            pid = uuid.uuid4().hex[:8]
            d = {"active": pid, "profiles": [dict(copy.deepcopy(DEFAULT_SETTINGS), id=pid)]}
            _write(_path("profiles.json"), d)
        d["profiles"] = [dict(_merge_defaults(p), id=p["id"]) for p in d["profiles"]]
        if d["active"] not in [p["id"] for p in d["profiles"]]:
            d["active"] = d["profiles"][0]["id"]
        return d


def save_profiles(d):
    with _lock:
        cur = _path("profiles.json")
        # резервная копия: раз в сутки отдельный файл, хранятся последние 10
        if os.path.exists(cur):
            bdir = _path("backup")
            os.makedirs(bdir, exist_ok=True)
            day = os.path.join(bdir, time.strftime("profiles-%Y%m%d.json"))
            if not os.path.exists(day):
                copy_file(cur, day)
                for old in sorted(os.listdir(bdir))[:-10]:
                    try:
                        os.remove(os.path.join(bdir, old))
                    except OSError:
                        pass
        _write(cur, d)


def get_profile(pid=None):
    d = load_profiles()
    pid = pid or d["active"]
    for p in d["profiles"]:
        if p["id"] == pid:
            return p
    return d["profiles"][0]


def save_profile(p):
    with _lock:
        d = load_profiles()
        for i, x in enumerate(d["profiles"]):
            if x["id"] == p["id"]:
                d["profiles"][i] = p
                break
        else:
            d["profiles"].append(p)
        save_profiles(d)


def new_profile(name, demo=False):
    with _lock:
        d = load_profiles()
        p = dict(copy.deepcopy(DEFAULT_SETTINGS), id=uuid.uuid4().hex[:8], name=name, demo=demo)
        d["profiles"].append(p)
        d["active"] = p["id"]
        save_profiles(d)
        return p


def delete_profile(pid):
    with _lock:
        d = load_profiles()
        d["profiles"] = [p for p in d["profiles"] if p["id"] != pid]
        if d["active"] == pid:
            d["active"] = d["profiles"][0]["id"] if d["profiles"] else None
        save_profiles(d)


def set_active(pid):
    with _lock:
        d = load_profiles()
        d["active"] = pid
        save_profiles(d)


# ---------------- партии поставок ----------------
def batch_dir(pid, bid):
    return _path("shops", pid, "batches", bid)


def new_batch_id():
    return time.strftime("%Y%m%d-%H%M%S")


def save_batch(pid, batch):
    with _lock:
        _write(os.path.join(batch_dir(pid, batch["id"]), "batch.json"), batch)


def load_batch(pid, bid):
    with _lock:
        return _read(os.path.join(batch_dir(pid, bid), "batch.json"), None)


def list_batches(pid):
    root = _path("shops", pid, "batches")
    out = []
    if os.path.isdir(root):
        for bid in sorted(os.listdir(root), reverse=True):
            b = load_batch(pid, bid)
            if b:
                out.append(b)
    return out
