"""Сборка PDF этикеток по одинаковым грузоместам и лист сборки."""
import html
import os
import re

from pypdf import PdfReader, PdfWriter


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", str(s)).strip()
    return re.sub(r"\s+", " ", s)[:120] or "file"


def merge(paths, out_path):
    w = PdfWriter()
    pages = 0
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        r = PdfReader(p)
        for page in r.pages:
            w.add_page(page)
            pages += 1
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        w.write(f)
    return pages


def build_outputs(batch, bdir):
    """Собирает PDF по группам, общий PDF и лист сборки. Возвращает batch['files']."""
    groups = batch.get("groups") or []
    ships = [s for s in batch["shipments"] if not s.get("cancelled")]
    out_dir = os.path.join(bdir, "Этикетки")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.lower().endswith(".pdf"):
            try:
                os.remove(os.path.join(out_dir, f))
            except OSError:
                pass

    files = {"groups": [], "all": None, "packing": None}
    all_paths = []
    for n, g in enumerate(groups, 1):
        paths = []
        missing = 0
        for s in ships:
            rel = (s.get("labels") or {}).get(g["id"])
            if rel:
                paths.append(os.path.join(bdir, rel))
            elif any(b["sku"] == g["sku"] and b["qty"] == g["qty"] for b in s.get("boxes", [])):
                missing += 1
        fname = safe_name(f"{n:02d} — {g['name']} × {g['qty']} шт — {g['total']} кор") + ".pdf"
        pages = merge(paths, os.path.join(out_dir, fname)) if paths else 0
        files["groups"].append({"id": g["id"], "file": f"Этикетки/{fname}" if paths else None,
                                "pages": pages, "missing": missing})
        all_paths += paths
    if all_paths:
        merge(all_paths, os.path.join(out_dir, "00 — Все этикетки.pdf"))
        files["all"] = "Этикетки/00 — Все этикетки.pdf"
    write_packing(batch, os.path.join(bdir, "Лист сборки.html"))
    files["packing"] = "Лист сборки.html"
    return files


def write_packing(batch, path):
    e = html.escape
    groups = batch.get("groups") or []
    ships = [s for s in batch["shipments"] if not s.get("cancelled")]
    rows = []
    for n, g in enumerate(groups, 1):
        cities = ", ".join(f"{e(c['name'])}{' ×' + str(c['count']) if c['count'] > 1 else ''}" for c in g["cities"])
        rows.append(f"<tr><td class=n>{n:02d}</td><td><b>{e(g['name'])}</b><div class=m>SKU {e(g['sku'])}</div></td>"
                    f"<td class=r><b>{g['qty']}</b> шт</td><td class=r><b>{g['total']}</b></td><td>{cities}</td>"
                    f"<td class=chk></td></tr>")
    city_rows = []
    for s in ships:
        nb = len(s.get("boxes", []))
        units = sum(b["qty"] for b in s.get("boxes", []))
        city_rows.append(f"<tr><td>{e(s['name'])}</td><td>{e(str(s.get('order_number') or s.get('order_id') or '—'))}</td>"
                         f"<td class=r>{nb}</td><td class=r>{units}</td><td class=chk></td></tr>")
    kits_html = ""
    kits = batch.get("kits")
    if kits and kits.get("defs"):
        prods = batch.get("products") or []
        pname = {p["sku"]: (p.get("short") or p.get("name") or p["sku"]) for p in prods}
        head = "".join(f"<th class=r>{e(pname.get(p['sku'], p['sku']))}</th>" for p in prods)
        krows = []
        for d in kits["defs"]:
            n = sum(int((a or {}).get(d["id"]) or 0) for a in kits["alloc"].values())
            cells = "".join(f"<td class=r>{int(d['items'].get(p['sku']) or 0)}</td>" for p in prods)
            krows.append(f"<tr><td><b>Набор {e(d['id'])}</b></td>{cells}<td class=r><b>{n}</b></td></tr>")
        names = {(s.get("key") or s["cluster_id"]): s["name"] for s in ships}
        crows = []
        for cid, a in kits["alloc"].items():
            if cid in names and any(int(v or 0) for v in (a or {}).values()):
                txt = " + ".join(f"{int(v)} × {e(k)}" for k, v in (a or {}).items() if int(v or 0))
                crows.append(f"<tr><td>{e(names[cid])}</td><td>{txt}</td><td class=chk></td></tr>")
        kits_html = (f"<h2>Наборы</h2><table><tr><th>Набор</th>{head}<th class=r>Наборов</th></tr>{''.join(krows)}</table>"
                     f"<table><tr><th>Кластер</th><th>Наборы</th><th>✓</th></tr>{''.join(crows)}</table>")
    slot = batch.get("chosen_slot_human") or ""
    doc = f"""<!doctype html><html lang=ru><meta charset=utf-8><title>Лист сборки {e(batch['id'])}</title>
<style>body{{font:13px/1.4 system-ui,Segoe UI,Arial;margin:24px;color:#111}}h1{{font-size:18px;margin:0 0 4px}}
.m{{color:#666;font-size:11px}}table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}
td,th{{border:1px solid #bbb;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f2f2f2;font-weight:600}}
.r{{text-align:right;white-space:nowrap}}.n{{width:28px;color:#666}}.chk{{width:40px}}h2{{font-size:15px;margin:16px 0 0}}
@media print{{body{{margin:8mm}}}}</style>
<h1>Лист сборки — партия {e(batch['id'])}</h1>
<div class=m>Точка отгрузки: {e((batch.get('dropoff') or {}).get('name') or '—')} · Таймслот: {e(slot)}</div>
{kits_html}
<h2>Коробки по составу</h2>
<table><tr><th>№</th><th>Товар</th><th class=r>В коробке</th><th class=r>Коробок</th><th>Города</th><th>✓</th></tr>
{''.join(rows)}</table>
<h2>По городам</h2>
<table><tr><th>Город</th><th>Заявка</th><th class=r>Коробок</th><th class=r>Штук</th><th>✓</th></tr>
{''.join(city_rows)}</table></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


# ================================================================ ПРОВЕРКА ЭТИКЕТОК
import hashlib  # noqa: E402


def cargo_ids_on_page(page, ids):
    """Какие из номеров грузомест напечатаны на странице (целым числом, не частью другого)."""
    try:
        raw = page.extract_text() or ""
    except Exception:
        raw = ""
    flat = re.sub(r"\s+", "", raw)
    out = []
    for c in ids:
        pat = rf"(?<!\d){int(c)}(?!\d)"
        if re.search(pat, raw) or re.search(pat, flat):
            out.append(int(c))
    return sorted(set(out))


def page_hash(page):
    """«Отпечаток» страницы: содержимое и картинки (штрихкод). Разные этикетки — разные отпечатки."""
    h = hashlib.sha1()
    try:
        data = page.get_contents()
        h.update(data.get_data() if data is not None else b"")
    except Exception:
        pass
    try:
        xo = (page.get("/Resources") or {}).get("/XObject") or {}
        for name in sorted(xo.keys()):
            try:
                h.update(xo[name].get_object().get_data())
            except Exception:
                h.update(str(name).encode())
    except Exception:
        pass
    return h.hexdigest()


def verify_outputs(batch, bdir):
    """Итоговая проверка файлов этикеток: каждая этикетка ровно один раз, в файле своего состава,
    страниц столько же, сколько грузомест."""
    ships = [s for s in batch["shipments"] if not s.get("cancelled") and s.get("status") == "done"]
    expected = {}          # номер грузоместа -> (состав, поставка)
    for s in ships:
        for b in s["boxes"]:
            cid = (s.get("cargo_ids") or {}).get(str(b["key"]))
            if cid:
                expected[int(cid)] = (f"{b['sku']}x{b['qty']}", s)
    all_ids = list(expected)
    groups = {g["id"]: g for g in batch.get("groups") or []}
    files = [f for f in (batch.get("files") or {}).get("groups") or [] if f.get("file")]
    problems, bad_ships = [], set()
    seen, hashes, pages_total, readable = {}, {}, 0, 0
    for f in files:
        gname = groups.get(f["id"], {}).get("name", f["id"])
        gqty = groups.get(f["id"], {}).get("qty", "")
        for n, page in enumerate(PdfReader(os.path.join(bdir, f["file"])).pages, 1):
            pages_total += 1
            where = f"«{gname} × {gqty} шт», стр. {n}"
            hp = page_hash(page)
            if hp in hashes:
                problems.append(f"{where}: такая же страница уже есть ({hashes[hp]})")
            hashes.setdefault(hp, where)
            ids = cargo_ids_on_page(page, all_ids)
            if not ids:
                continue
            readable += 1
            if len(ids) > 1:
                problems.append(f"{where}: на странице несколько номеров грузомест")
                continue
            c = ids[0]
            if c in seen:
                problems.append(f"{where}: этикетка грузоместа {c} повторяется (уже была: {seen[c]})")
                bad_ships.add(expected[c][1].get("key") or expected[c][1]["cluster_id"])
            seen.setdefault(c, where)
            if expected[c][0] != f["id"]:
                problems.append(f"{where}: этикетка грузоместа {c} не из этого состава")
                bad_ships.add(expected[c][1].get("key") or expected[c][1]["cluster_id"])
    if pages_total != len(expected):
        problems.insert(0, f"страниц {pages_total}, а грузомест {len(expected)}")
    if readable:
        for c, (gid, s) in expected.items():
            if c not in seen:
                g = groups.get(gid, {})
                problems.append(f"нет этикетки грузоместа {c} ({s['name']}, {g.get('name', gid)} × {g.get('qty', '')} шт)")
                bad_ships.add(s.get("key") or s["cluster_id"])
    how = "по номерам грузомест" if readable else "по изображению страниц (номера не читаются)"
    ok = not problems
    summary = (f"{len(expected)} этикеток — все разные, по одной на каждое грузоместо (проверено {how})" if ok
               else f"найдено проблем: {len(problems)}")
    return {"ok": ok, "summary": summary, "problems": problems[:50], "bad_ships": sorted(bad_ships),
            "boxes": len(expected), "pages": pages_total, "at": __import__("time").strftime("%d.%m %H:%M")}


# ---------------- штрихкоды товаров (этикетка товара из кабинета Озона)
def barcode_plan(batch, sku):
    """Сколько штук товара едет в каждый город: [(город, шт)], по возрастанию количества."""
    sku = str(sku)
    out = []
    for n, s in enumerate(batch.get("shipments") or []):
        if s.get("cancelled"):
            continue
        q = int((s.get("items") or {}).get(sku) or 0)
        if q > 0:
            out.append((q, n, s.get("name") or ""))
    return [(name, q) for q, _, name in sorted(out)]


def check_label_pdf(path):
    """Загруженный файл — PDF хотя бы с одной страницей. Возвращает размер страницы в мм."""
    r = PdfReader(path)
    if not r.pages:
        raise ValueError("в файле нет страниц")
    box = r.pages[0].mediabox
    return round(float(box.width) / 72 * 25.4), round(float(box.height) / 72 * 25.4)


_FONT = None


def _font():
    """Шрифт с кириллицей для информационных этикеток (лежит в программе: fonts/)."""
    global _FONT
    if _FONT is None:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import sys
        root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        pdfmetrics.registerFont(TTFont("DejaVuBold", os.path.join(root, "fonts", "DejaVuSans-Bold.ttf")))
        _FONT = "DejaVuBold"
    return _FONT


def info_page(width, height, lines):
    """Информационная этикетка размером с этикетку товара: строки крупно, по центру.
    lines — [(текст, доля высоты этикетки)]. Возвращает страницу pypdf."""
    import io
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas
    font = _font()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    pad = min(width, height) * 0.07
    sizes = []
    for text, share in lines:
        size = height * share
        tw = stringWidth(text, font, size)
        if tw > width - 2 * pad:                   # длинное — уменьшаем, чтобы влезло по ширине
            size *= (width - 2 * pad) / tw
        sizes.append(size)
    gap = height * 0.05
    total = sum(sizes) + gap * (len(lines) - 1)
    y = (height + total) / 2
    for (text, _), size in zip(lines, sizes):
        y -= size
        c.setFont(font, size)
        c.drawCentredString(width / 2, y + size * 0.18, text)
        y -= gap
    c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def build_barcodes(label_pdf, plan, out_path, short=""):
    """Этикетка товара (первая страница файла) × количество для каждого города.
    Перед каждым городом — информационная этикетка того же размера: товар коротко, сколько штук, город.
    Возвращает число страниц."""
    page = PdfReader(label_pdf).pages[0]
    w_, h_ = float(page.mediabox.width), float(page.mediabox.height)
    w = PdfWriter()
    pages = 0
    for city, q in plan:
        w.add_page(info_page(w_, h_, [(short, 0.26), (f"{q} шт", 0.26), (city, 0.17)] if short
                             else [(f"{q} шт", 0.30), (city, 0.20)]))
        pages += 1
        for _ in range(q):
            w.add_page(page)
            pages += 1
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        w.write(f)
    return pages
