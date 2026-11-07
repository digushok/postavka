import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import slots as S

def sl(d, h): return [f"2026-11-{d:02d}T{h:02d}:00:00Z", f"2026-11-{d:02d}T{h+3:02d}:00:00Z"]

class T(unittest.TestCase):
    def test_pick_inside(self):
        slots = [sl(9, 9), sl(9, 12), sl(10, 9), sl(11, 12)]
        pref = {"date_from": "2026-11-09", "date_to": "2026-11-11", "time_from": "11:00", "time_to": "14:00"}
        self.assertEqual(S.pick(slots, pref), sl(9, 12))

    def test_pick_nearest_outside(self):
        slots = [sl(5, 9), sl(20, 9)]
        pref = {"date_from": "2026-11-09", "date_to": "2026-11-10", "time_from": "09:00", "time_to": "18:00"}
        self.assertEqual(S.pick(slots, pref), sl(5, 9))

    def test_common(self):
        a = [sl(9, 9), sl(9, 12)]; b = [sl(9, 12), sl(10, 9)]
        self.assertEqual(S.common_slots([a, b]), [sl(9, 12)])
        self.assertEqual(S.nearest_to(b, sl(9, 9)), sl(9, 12))

if __name__ == "__main__":
    unittest.main()
