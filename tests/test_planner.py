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


if __name__ == "__main__":
    unittest.main()


class Kits(unittest.TestCase):
    """Пример пользователя: закупка 4 100 шт, доли кратны 2,5%."""
    P = [{"sku": s, "qty": q} for s, q in
         [("32", 500), ("64", 700), ("128", 1200), ("256", 800), ("512", 400), ("1T", 300), ("2T", 200)]]
    R = [{"cluster_id": c, "share": s} for c, s in
         [("msk", 30), ("spb", 12.5), ("rnd", 7.5), ("ekb", 7.5), ("ufa", 7.5), ("kzn", 5), ("vrn", 5), ("krd", 5),
          ("yar", 5), ("srt", 5), ("nev", 5), ("prm", 2.5), ("orb", 2.5), ("tvr", 0)]]

    def test_user_example(self):
        r = P.auto_kits(self.P, self.R, remainder_cluster="msk")
        d = {k["id"]: k["items"] for k in r["defs"]}
        self.assertEqual([d["А"][s] for s in ["32", "64", "128", "256", "512", "1T", "2T"]], [25, 35, 60, 40, 20, 15, 10])
        self.assertEqual([d["Б"][s] for s in ["32", "64", "128", "256", "512", "1T", "2T"]], [12, 17, 30, 20, 10, 7, 5])
        self.assertEqual([d["В"][s] for s in ["32", "64", "128", "256", "512", "1T", "2T"]], [28, 38, 60, 40, 20, 18, 10])
        a = r["alloc"]
        self.assertEqual(a["msk"], {"А": 5, "Б": 0, "В": 1})
        self.assertEqual(a["spb"], {"А": 2, "Б": 1})
        self.assertEqual(a["prm"], {"А": 0, "Б": 1})
        self.assertEqual(a["tvr"], {"А": 0, "Б": 0})
        self.assertEqual(sum(v.get("А", 0) for v in a.values()), 16)
        self.assertEqual(sum(v.get("Б", 0) for v in a.values()), 6)
        m = P.kits_matrix(r["defs"], a, self.P)
        for p in self.P:
            self.assertEqual(sum(m[c][p["sku"]] for c in m), p["qty"])
        boxes = P.build_boxes_kits(r["defs"], a, self.P, [x["cluster_id"] for x in self.R])
        self.assertEqual(sum(len(b) for b in boxes.values()), 23 * 7)
        g = {x["id"]: x["total"] for x in P.group_boxes(boxes, self.P, [x["cluster_id"] for x in self.R])}
        self.assertEqual(g["32x25"], 16)

    def test_steps_sum(self):
        st = P.round_shares_to_steps([{"cluster_id": i, "share": s} for i, s in enumerate([29, 11.5, 1.5, 58])])
        self.assertEqual(sum(st.values()), 40)
