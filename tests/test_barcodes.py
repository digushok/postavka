import os
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter

import labels as L


def batch():
    ships = [{"name": f"Город {i}", "items": {"111": 20}} for i in range(10)]
    ships += [{"name": "Казань", "items": {"111": 40, "222": 5}}, {"name": "Уфа", "items": {"111": 40}},
              {"name": "Москва", "items": {"111": 100}}, {"name": "Отменён", "items": {"111": 7}, "cancelled": True}]
    return {"shipments": ships}


class Barcodes(unittest.TestCase):
    def test_plan_ascending_without_cancelled(self):
        plan = L.barcode_plan(batch(), 111)
        self.assertEqual([q for _, q in plan], [20] * 10 + [40, 40, 100])
        self.assertEqual(plan[0][0], "Город 0")
        self.assertEqual(L.barcode_plan(batch(), "222"), [("Казань", 5)])

    def test_failed_city_excluded(self):
        b = batch()
        b["shipments"][10].update(status="error")                       # Казань: Озон не принял заявку
        self.assertNotIn("Казань", [c for c, _ in L.barcode_plan(b, "111")])
        self.assertEqual(L.failed_cities(b, "111"), ["Казань"])
        self.assertEqual(L.barcode_plan(b, "222"), [])
        b["shipments"][10].update(status="done", order_id=7)            # после повтора — снова в файле
        self.assertIn("Казань", [c for c, _ in L.barcode_plan(b, "111")])

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
        first = r.pages[0].extract_text()                     # файл начинается с информационной этикетки
        self.assertIn("32 ГБ", first)
        self.assertIn("20 шт", first)
        self.assertIn("Город 0", first)
        self.assertIn("Москва", r.pages[n - 101].extract_text())   # перед последними 100 штрихкодами
        self.assertEqual(r.pages[n - 1].extract_text().strip(), "")  # в конце — штрихкод, не инфо-этикетка
        self.assertEqual(round(float(r.pages[0].mediabox.width) / 72 * 25.4), 58)




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
        paths, missing = L.cargo_plan(b, d, "111")
        self.assertEqual([os.path.basename(p) for p in paths], ["k20.pdf", "u20.pdf", "m20.pdf", "m5.pdf"])
        self.assertEqual(missing, 1)
        out = os.path.join(d, "out.pdf")
        self.assertEqual(L.merge(paths, out), 5)
        self.assertEqual([os.path.basename(p) for p in L.cargo_plan(b, d, "222")[0]], ["m4.pdf"])


if __name__ == "__main__":
    unittest.main()
