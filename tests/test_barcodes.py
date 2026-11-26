import os
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter

import labels as L


def batch():
    ships = [{"name": f"Город {i}", "items": {"111": 20}} for i in range(10)]
    ships += [{"name": "Казань", "items": {"111": 40, "222": 5}}, {"name": "Уфа", "items": {"111": 40}},
              {"name": "Москва", "items": {"111": 100}}, {"name": "Отменён", "items": {"111": 7}, "cancelled": True}]
    for n, s in enumerate(ships):
        s["order_id"] = 1000 + n                                      # заявки созданы
    return {"shipments": ships}


class Barcodes(unittest.TestCase):
    def test_plan_ascending_without_cancelled(self):
        plan = L.barcode_plan(batch(), 111)
        self.assertEqual([q for _, q in plan], [20] * 10 + [40, 40, 100])
        self.assertEqual(plan[0][0], "Город 0")
        self.assertEqual(L.barcode_plan(batch(), "222"), [("Казань", 5)])

    def test_failed_city_excluded(self):
        b = batch()
        b["shipments"][10].update(status="error", order_id=None)        # Казань: Озон не принял заявку
        self.assertNotIn("Казань", [c for c, _ in L.barcode_plan(b, "111")])
        self.assertEqual(L.failed_cities(b, "111"), ["Казань"])
        self.assertEqual(L.barcode_plan(b, "222"), [])
        b["shipments"][10].update(status="done", order_id=7)            # после повтора — снова в файле
        self.assertIn("Казань", [c for c, _ in L.barcode_plan(b, "111")])

    def test_city_in_progress_not_yet_in_file(self):
        b = batch()
        b["shipments"][11].update(status="draft", order_id=None)        # Уфа: заявка ещё создаётся
        self.assertNotIn("Уфа", [c for c, _ in L.barcode_plan(b, "111")])
        self.assertEqual(L.failed_cities(b, "111"), ["Уфа"])
        sig = L.plan_sig(L.barcode_plan(b, "111"))
        b["shipments"][11].update(status="supply", order_id=9)          # заявка создана — город в файле
        self.assertIn(("Уфа", 40), L.barcode_plan(b, "111"))
        self.assertNotEqual(L.plan_sig(L.barcode_plan(b, "111")), sig)  # «состав изменился после отметки»

    def test_pdf_pages_and_blanks(self):
        d = tempfile.mkdtemp()
        label = os.path.join(d, "label.pdf")
        w = PdfWriter()
        w.add_blank_page(58 / 25.4 * 72, 40 / 25.4 * 72)
        w.add_blank_page(58 / 25.4 * 72, 40 / 25.4 * 72)      # лишние страницы файла не берутся
        with open(label, "wb") as f:
            w.write(f)
        self.assertEqual(L.check_label_pdf(label), (58, 40))
        plan = L.barcode_plan(batch(), "111")
        out = os.path.join(d, "out.pdf")
        n = L.build_barcodes(label, plan, out, "32 ГБ")
        self.assertEqual(n, 380 + 13)                          # 380 штрихкодов и 13 информационных этикеток
        r = PdfReader(out)
        self.assertEqual(len(r.pages), n)
        # порядок обратный: файл кончается информационной этикеткой первого (самого маленького) города
        last = r.pages[n - 1].extract_text()
        self.assertIn("32 ГБ", last)
        self.assertIn("20 шт", last)
        self.assertIn("Город 0", last)
        self.assertEqual(r.pages[0].extract_text().strip(), "")     # в начале — штрихкоды Москвы
        self.assertIn("Москва", r.pages[100].extract_text())        # после 100 штрихкодов Москвы — её этикетка
        self.assertEqual(round(float(r.pages[0].mediabox.width) / 72 * 25.4), 58)
        self.assertTrue(all(pg.rotation == 180 for pg in r.pages))     # каждая страница вверх ногами




class CargoBySku(unittest.TestCase):
    def test_cargo_order_and_missing(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "raw"))

        def pdf(name, pages):
            w = PdfWriter()
            for _ in range(pages):
                w.add_blank_page(200, 300)
            with open(os.path.join(d, "raw", name), "wb") as f:
                w.write(f)
            return f"raw/{name}"

        b = {"groups": [{"id": "111x20", "sku": "111"}, {"id": "111x5", "sku": "111"}, {"id": "222x4", "sku": "222"}],
             "shipments": [
                 {"name": "Москва", "items": {"111": 45, "222": 4},
                  "boxes": [{"sku": "111", "qty": 20}, {"sku": "111", "qty": 20}, {"sku": "111", "qty": 5}, {"sku": "222", "qty": 4}],
                  "labels": {"111x20": pdf("m20.pdf", 2), "111x5": pdf("m5.pdf", 1), "222x4": pdf("m4.pdf", 1)}},
                 {"name": "Казань", "items": {"111": 20}, "boxes": [{"sku": "111", "qty": 20}],
                  "labels": {"111x20": pdf("k20.pdf", 1)}},
                 {"name": "Уфа", "items": {"111": 25}, "boxes": [{"sku": "111", "qty": 20}, {"sku": "111", "qty": 5}],
                  "labels": {"111x20": pdf("u20.pdf", 1)}},           # этикетку 111x5 ещё не получили
                 {"name": "Отменён", "items": {"111": 1}, "cancelled": True, "boxes": [{"sku": "111", "qty": 1}]},
             ]}
        for n, s in enumerate(b["shipments"]):
            s["order_id"] = 500 + n                                   # заявки созданы
        paths, missing = L.cargo_plan(b, d, "111")
        self.assertEqual([(os.path.basename(p), q) for p, q in paths],
                         [("k20.pdf", 20), ("u20.pdf", 20), ("m20.pdf", 20), ("m5.pdf", 5)])
        self.assertEqual(missing, 1)
        out = os.path.join(d, "out.pdf")
        self.assertEqual(L.build_cargo(paths, out, "32 ГБ"), 6)          # информационный лист + 5 этикеток
        r = PdfReader(out)
        self.assertIn("32 ГБ", r.pages[5].extract_text())                 # порядок обратный — лист последний
        self.assertTrue(all(pg.rotation == 180 for pg in r.pages))
        qty = [pg.extract_text().strip() for pg in r.pages[:5]][::-1]    # обратно к прямому порядку
        self.assertEqual(qty, ["20 шт", "20 шт", "20 шт", "20 шт", "5 шт"])
        self.assertEqual((float(r.pages[5].mediabox.width), float(r.pages[5].mediabox.height)), (200.0, 300.0))
        self.assertEqual([(os.path.basename(p), q) for p, q in L.cargo_plan(b, d, "222")[0]], [("m4.pdf", 4)])


if __name__ == "__main__":
    unittest.main()
