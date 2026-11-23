"""Таблица «кластеры × SKU» в Excel: шаблон для заполнения и чтение заполненного файла.

Без сторонних библиотек: .xlsx — это zip с XML внутри. Читается первый лист, понимаются
строки (общие и встроенные) и числа — этого достаточно для таблицы количеств.
Также читается .csv (разделитель ; , или табуляция; UTF-8 или Windows-1251).

Формат таблицы (шаблон):
    A1 «Кластер \\ SKU», B1… — SKU Озона
    строка «Товар»           — названия (для удобства, при импорте не читаются)
    строка «В коробке, шт»   — сколько штук в коробке (необязательно)
    дальше строки кластеров: в A — название кластера Озона, в ячейках — штук этого SKU в кластер
"""
import csv
import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}

HEAD = "Кластер \\ SKU"
ROW_NAME = "Товар"
ROW_BOX = "В коробке, шт"


class TableError(ValueError):
    pass


# ---------------------------------------------------------------- чтение
def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data):
    """Первый лист .xlsx → список строк (список значений: str, число или None)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TableError("Файл не похож на .xlsx. Старый формат .xls не поддерживается — сохраните его как .xlsx")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    # путь к первому листу — через workbook.xml и его связи
    sheet = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = wb.find("m:sheets/m:sheet", NS)
        rid = first.get(f"{{{NS['r']}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.findall("pr:Relationship", NS):
            if rel.get("Id") == rid:
                t = rel.get("Target").lstrip("/")
                sheet = t if t.startswith("xl/") else "xl/" + t
    except (KeyError, AttributeError, ET.ParseError):
        pass
    try:
        root = ET.fromstring(z.read(sheet))
    except KeyError:
        raise TableError("В файле не найден лист с таблицей")
    rows = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", NS):
            ref, t = c.get("r"), c.get("t")
            v = c.find("m:v", NS)
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
            elif v is not None and v.text is not None:
                val = v.text
                if t not in ("str", "e"):
                    try:
                        f = float(val)
                        val = int(f) if f.is_integer() else f
                    except ValueError:
                        pass
            else:
                continue
            idx = _col_index(ref) if ref else len(cells)
            cells[idx] = val
        idx_row = int(row.get("r") or len(rows) + 1) - 1
        while len(rows) < idx_row:
            rows.append([])
        rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return rows


def read_csv(data):
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise TableError("Не удалось прочитать текст файла")
    sample = text[:2000]
    delim = "\t" if "\t" in sample else (";" if sample.count(";") >= sample.count(",") else ",")
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def _num(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(round(v))
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not s or s in ("-", "—"):
        return 0
    try:
        return int(round(float(s)))
    except ValueError:
        raise TableError(f"Не число: «{v}»")


def _sku(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v if v is not None else "").strip()
    return s if re.fullmatch(r"\d{3,20}", s) else None


def parse_matrix(rows):
    """Строки таблицы → {"products": [{sku, qty, box_size}], "rows": [{name, cells: {sku: шт}}], "skipped": [...]}."""
    head_i = None
    for i, r in enumerate(rows):
        if sum(1 for v in r[1:] if _sku(v)) >= 1:
            head_i = i
            break
    if head_i is None:
        raise TableError("Не найдена строка с SKU: в первой строке таблицы, начиная со второго столбца, должны быть SKU Озона")
    head = rows[head_i]
    cols = [(j, _sku(v)) for j, v in enumerate(head) if j > 0 and _sku(v)]
    if len({s for _, s in cols}) != len(cols):
        raise TableError("Один и тот же SKU встречается в двух столбцах")
    boxes, out, skipped = {}, [], []
    for i, r in enumerate(rows[head_i + 1:], head_i + 2):
        name = str(r[0]).strip() if r and r[0] is not None else ""
        low = name.lower()
        if not name and not any(v not in (None, "") for v in r[1:]):
            continue
        if low.startswith(ROW_NAME.lower()):
            continue
        if low.startswith("в коробке"):
            for j, sku in cols:
                q = _num(r[j] if j < len(r) else None)
                if q > 0:
                    boxes[sku] = q
            continue
        if low.startswith(("итог", "всего")):
            continue
        if not name:
            skipped.append(f"строка {i}: нет названия кластера")
            continue
        try:
            cells = {sku: _num(r[j] if j < len(r) else None) for j, sku in cols}
        except TableError as e:
            raise TableError(f"Строка {i} ({name}): {e}")
        if any(q < 0 for q in cells.values()):
            raise TableError(f"Строка {i} ({name}): отрицательное количество")
        if not any(cells.values()):
            continue                                  # кластер без товара — не нужен
        if any(x["name"].lower() == low for x in out):
            raise TableError(f"Кластер «{name}» встречается дважды")
        out.append({"name": name, "cells": cells})
    if not out:
        raise TableError("В таблице нет ни одного кластера с количеством")
    products = [{"sku": sku, "qty": sum(x["cells"][sku] for x in out), "box_size": boxes.get(sku)} for _, sku in cols]
    products = [p for p in products if p["qty"] > 0]
    return {"products": products, "rows": out, "skipped": skipped}


def read_table(name, data):
    name = (name or "").lower()
    if name.endswith((".csv", ".txt", ".tsv")):
        rows = read_csv(data)
    elif name.endswith(".xls"):
        raise TableError("Старый формат .xls не поддерживается — в Excel: Файл → Сохранить как → Книга Excel (.xlsx)")
    else:
        rows = read_xlsx(data)
    return parse_matrix(rows)


# ---------------------------------------------------------------- шаблон
def _col_name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v, style=0):
    st = f' s="{style}"' if style else ""
    if v is None or v == "":
        return f'<c r="{ref}"{st}/>' if style else ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f'<c r="{ref}"{st}><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'


def _sheet(rows, widths, freeze=None):
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           f'<worksheet xmlns="{NS["m"]}" xmlns:r="{NS["r"]}">']
    if freeze:
        xml.append(f'<sheetViews><sheetView workbookViewId="0"><pane xSplit="{freeze[0]}" ySplit="{freeze[1]}" '
                   f'topLeftCell="{_col_name(freeze[0])}{freeze[1] + 1}" activePane="bottomRight" state="frozen"/>'
                   f'</sheetView></sheetViews>')
    xml.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                                  for i, w in enumerate(widths)) + "</cols><sheetData>")
    for i, row in enumerate(rows, 1):
        cells = "".join(_cell(f"{_col_name(j)}{i}", v, s) for j, (v, s) in enumerate(row))
        xml.append(f'<row r="{i}">{cells}</row>')
    xml.append("</sheetData></worksheet>")
    return "".join(xml)


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<styleSheet xmlns="{NS["m"]}">'
          '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
          '<font><b/><sz val="11"/><name val="Calibri"/></font>'
          '<font><i/><sz val="10"/><color rgb="FF7B8092"/><name val="Calibri"/></font></fonts>'
          '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFEEF0F7"/></patternFill></fill></fills>'
          '<borders count="1"><border/></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'  # 1 — заголовок
          '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'                # 2 — подсказка
          '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'                # 3 — жирный
          '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
          '</styleSheet>')


def build_template(products, clusters, all_clusters=()):
    """products: [{sku, name, box_size}]; clusters: названия кластеров для строк; all_clusters — справочник."""
    if not products:
        products = [{"sku": "", "name": "", "box_size": None}] * 3
    head = [(HEAD, 1)] + [(int(p["sku"]) if str(p["sku"]).isdigit() else "", 1) for p in products]
    names = [(ROW_NAME, 2)] + [(p.get("name") or "", 2) for p in products]
    box = [(ROW_BOX, 2)] + [(int(p["box_size"]) if p.get("box_size") else None, 0) for p in products]
    rows = [head, names, box] + [[(c, 3)] + [(None, 0)] * len(products) for c in (clusters or ["Москва"])]
    sheet1 = _sheet(rows, [34] + [16] * len(products), freeze=(1, 3))
    help_rows = [[(t, s)] for t, s in [
        ("Как заполнить", 1),
        ("1. В первой строке — SKU Озона, по одному в столбце. Новые SKU добавляйте правее.", 0),
        ("2. Строка «Товар» — только для удобства, программа её не читает.", 0),
        ("3. Строка «В коробке, шт» — сколько штук в коробке. Можно оставить пустой: возьмётся из настроек товара.", 0),
        ("4. Ниже — по строке на кластер: в первом столбце название кластера Озона (как на листе «Кластеры Озона»),", 0),
        ("   в ячейках — сколько штук этого SKU едет в кластер. Пусто или 0 — не едет.", 0),
        ("5. В программе: «Новая поставка» → «Импорт таблицы» → выберите этот файл.", 0),
        ("   Товары и таблица распределения заполнятся из файла.", 0),
    ]]
    sheet2 = _sheet(help_rows, [110])
    sheets = [("Распределение", sheet1), ("Как заполнить", sheet2)]
    if all_clusters:
        sheets.append(("Кластеры Озона", _sheet([[("Кластер Озона", 1)]] + [[(c, 0)] for c in all_clusters], [50])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="{NS["pr"]}">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<workbook xmlns="{NS["m"]}" xmlns:r="{NS["r"]}"><sheets>'
                   + "".join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _) in enumerate(sheets, 1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="{NS["pr"]}">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   + "</Relationships>")
        z.writestr("xl/styles.xml", STYLES)
        for i, (_, xml) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    return buf.getvalue()
