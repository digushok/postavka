import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import planner as P

CITIES = [("msk", 25), ("spb", 10), ("rnd", 10)] + [(f"c{i}", 5) for i in range(11)]
CITIES = [{"cluster_id": c, "share": s} for c, s in CITIES]
PRODUCTS = [{"sku": s, "qty": q} for s, q in
            [("64", 600), ("512", 300), ("32", 300), ("256", 600), ("2048", 200), ("128", 1000), ("1024", 300)]]


class T(unittest.TestCase):
    def test_excel_example(self):
        m, un = P.distribute(PRODUCTS, CITIES, P.METHOD_REMAINDER_CITY, "msk")
        self.assertEqual(m["msk"]["64"], 150)
        self.assertEqual(m["spb"]["128"], 100)
        self.assertEqual(m["c3"]["64"], 30)
        self.assertEqual(m["c3"]["512"], 15)
        self.assertEqual(un, {})
        for p in PRODUCTS:
            self.assertEqual(sum(m[c["cluster_id"]][p["sku"]] for c in CITIES), p["qty"])

    def test_remainder_methods(self):
        prods = [{"sku": "a", "qty": 103}]
        m, _ = P.distribute(prods, CITIES, P.METHOD_REMAINDER_CITY, "msk")
        self.assertEqual(sum(v["a"] for v in m.values()), 103)
        self.assertEqual(m["c0"]["a"], 5)
        m, _ = P.distribute(prods, CITIES, P.METHOD_FAIR)
        self.assertEqual(sum(v["a"] for v in m.values()), 103)
        m, un = P.distribute(prods, CITIES, P.METHOD_FLOOR)
        self.assertEqual(sum(v["a"] for v in m.values()) + un["a"], 103)

    def test_box_multiple(self):
        prods = [{"sku": "cam", "qty": 480, "box_size": 24, "multiple": True}]
        m, _ = P.distribute(prods, CITIES, P.METHOD_REMAINDER_CITY, "msk")
        for c in CITIES:
            if c["cluster_id"] != "msk":
                self.assertEqual(m[c["cluster_id"]]["cam"] % 24, 0)
        self.assertEqual(sum(v["cam"] for v in m.values()), 480)

    def test_split(self):
        self.assertEqual(P.split_boxes(96, 24), [24, 24, 24, 24])
        self.assertEqual(P.split_boxes(100, 24), [24, 24, 24, 24, 4])
        self.assertEqual(P.split_boxes(250, None), [250])
        self.assertEqual(P.split_boxes(0, 24), [])

    def test_groups(self):
        m, _ = P.distribute(PRODUCTS, CITIES, P.METHOD_REMAINDER_CITY, "msk")
        order = [c["cluster_id"] for c in CITIES]
        boxes = P.build_boxes(m, PRODUCTS, order)
        self.assertEqual(len(boxes["c0"]), 7)
        groups = P.group_boxes(boxes, PRODUCTS, order)
        g = next(g for g in groups if g["id"] == "64x30")
        self.assertEqual(g["total"], 11)
        g = next(g for g in groups if g["id"] == "64x60")
        self.assertEqual(g["total"], 2)
        self.assertEqual(sum(g["total"] for g in groups), 14 * 7)



class Layout(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(P.parse_layout("10×24+14+10"), [[10, 24], [1, 14], [1, 10]])
        self.assertEqual(P.parse_layout("10*24 + 14"), [[10, 24], [1, 14]])
        self.assertEqual(P.parse_layout("3 по 20"), [[3, 20]])
        self.assertIsNone(P.parse_layout("264"))
        with self.assertRaises(ValueError):
            P.parse_layout("abc+1")

    def test_boxes_follow_layout(self):
        prods = [{"sku": "1", "qty": 300, "box_size": 24}, {"sku": "2", "qty": 50, "box_size": 20}]
        m = {"msk": {"1": 264, "2": 50}}
        b = P.build_boxes(m, prods, ["msk"], {"msk": {"1": [[10, 24], [1, 14], [1, 10]]}})["msk"]
        self.assertEqual([x["qty"] for x in b if x["sku"] == "1"], [24] * 10 + [14, 10])
        self.assertEqual([x["qty"] for x in b if x["sku"] == "2"], [20, 20, 10])        # без раскладки — как раньше
        self.assertEqual(len({x["key"] for x in b}), len(b))
        b = P.build_boxes({"msk": {"1": 100}}, prods, ["msk"], {"msk": {"1": [[10, 24]]}})["msk"]
        self.assertEqual([x["qty"] for x in b], [24, 24, 24, 24, 4])                   # сумма не сходится — раскладка не берётся


if __name__ == "__main__":
    unittest.main()
