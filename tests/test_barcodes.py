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
        n = L.build_barcodes(label, plan, out)
        self.assertEqual(n, 380 + 12)                          # 380 штрихкодов и 12 пустых между городами
        self.assertEqual(len(PdfReader(out).pages), n)


if __name__ == "__main__":
    unittest.main()
