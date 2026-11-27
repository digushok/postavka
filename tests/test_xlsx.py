import unittest

import xlsx as X


class Template(unittest.TestCase):
    def test_roundtrip(self):
        data = X.build_template([{"sku": "1001", "name": "32 ГБ", "box_size": 20}, {"sku": "1002", "name": "64 ГБ"}],
                                ["Москва", "Казань"], ["Москва, МО и Дальние регионы", "Казань"])
        rows = X.read_xlsx(data)
        self.assertEqual(rows[0][:3], [X.HEAD, 1001, 1002])
        self.assertEqual(rows[3][0], "Москва")
        # пустой шаблон — кластеров с количеством нет
        with self.assertRaises(X.TableError):
            X.parse_matrix(rows)
        rows[3] += [None] * 2
        rows[3][1], rows[3][2] = 100, 40
        rows[4] += [None] * 2
        rows[4][1] = 20
        t = X.parse_matrix(rows)
        self.assertEqual(t["rows"], [{"name": "Москва", "cells": {"1001": 100, "1002": 40}},
                                     {"name": "Казань", "cells": {"1001": 20, "1002": 0}}])
        self.assertEqual(t["products"], [{"sku": "1001", "qty": 120, "box_size": 20},
                                         {"sku": "1002", "qty": 40, "box_size": None}])


class Parse(unittest.TestCase):
    def test_csv_semicolon_cp1251_and_junk(self):
        text = "Кластер;1001;1002\r\nВ коробке, шт;10;\r\nМосква;1 000;5\r\n;;\r\nУфа;0;0\r\nИтого;1000;5\r\n"
        t = X.read_table("a.csv", text.encode("cp1251"))
        self.assertEqual(t["rows"], [{"name": "Москва", "cells": {"1001": 1000, "1002": 5}}])
        self.assertEqual(t["products"][0]["box_size"], 10)

    def test_errors(self):
        with self.assertRaises(X.TableError):
            X.parse_matrix([["Кластер", "1001", "1001"], ["Москва", 1, 2]])          # SKU дважды
        with self.assertRaises(X.TableError):
            X.parse_matrix([["Кластер", "1001"], ["Москва", "много"]])                # не число
        with self.assertRaises(X.TableError):
            X.parse_matrix([["Кластер", "1001"], ["Москва", 1], ["москва", 2]])       # кластер дважды
        with self.assertRaises(X.TableError):
            X.read_table("a.xls", b"\xd0\xcf\x11\xe0")



class LayoutCells(unittest.TestCase):
    def test_layout_in_cell(self):
        t = X.parse_matrix([[X.HEAD, "1001", "1002"], ["Москва", "10×24+14+10", 5], ["Казань", "3*20", ""]])
        self.assertEqual(t["rows"][0]["cells"], {"1001": 264, "1002": 5})
        self.assertEqual(t["rows"][0]["layout"], {"1001": [[10, 24], [1, 14], [1, 10]]})
        self.assertEqual(t["rows"][1]["cells"]["1001"], 60)
        with self.assertRaises(X.TableError):
            X.parse_matrix([[X.HEAD, "1001"], ["Москва", "10×abc"]])


if __name__ == "__main__":
    unittest.main()
