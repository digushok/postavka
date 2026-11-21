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


if __name__ == "__main__":
    unittest.main()
