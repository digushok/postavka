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
    slot = batch.get("chosen_slot_human") or ""
    doc = f"""<!doctype html><html lang=ru><meta charset=utf-8><title>Лист сборки {e(batch['id'])}</title>
<style>body{{font:13px/1.4 system-ui,Segoe UI,Arial;margin:24px;color:#111}}h1{{font-size:18px;margin:0 0 4px}}
.m{{color:#666;font-size:11px}}table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}
td,th{{border:1px solid #bbb;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f2f2f2;font-weight:600}}
.r{{text-align:right;white-space:nowrap}}.n{{width:28px;color:#666}}.chk{{width:40px}}h2{{font-size:15px;margin:16px 0 0}}
@media print{{body{{margin:8mm}}}}</style>
<h1>Лист сборки — партия {e(batch['id'])}</h1>
<div class=m>Точка отгрузки: {e((batch.get('dropoff') or {}).get('name') or '—')} · Таймслот: {e(slot)}</div>
<h2>Коробки по составу</h2>
<table><tr><th>№</th><th>Товар</th><th class=r>В коробке</th><th class=r>Коробок</th><th>Города</th><th>✓</th></tr>
{''.join(rows)}</table>
<h2>По городам</h2>
<table><tr><th>Город</th><th>Заявка</th><th class=r>Коробок</th><th class=r>Штук</th><th>✓</th></tr>
{''.join(city_rows)}</table></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
