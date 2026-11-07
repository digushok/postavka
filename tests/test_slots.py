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



class TestPerCity(unittest.TestCase):
    pref = {"date_from": "2026-11-09", "date_to": "2026-11-11", "time_from": "00:00", "time_to": "23:59"}

    def test_example_from_user(self):
        # нужен 9–11 число: у части городов есть 9-е, у части 11-е, у остальных только 15-е
        a = [sl(9, 10), sl(15, 10)]
        b = [sl(11, 10), sl(15, 10)]
        c = [sl(15, 10), sl(17, 10)]
        cov = {S.key(s): n for s, n in S.slot_counts([a, b, c])}
        self.assertEqual(S.choose_for_city(a, self.pref, cov), sl(9, 10))
        self.assertEqual(S.choose_for_city(b, self.pref, cov), sl(11, 10))
        self.assertEqual(S.choose_for_city(c, self.pref, cov), sl(15, 10))

    def test_earliest_day_first_then_most_shared_time(self):
        a = [sl(9, 9), sl(9, 12), sl(10, 9)]
        others = [[sl(9, 12)], [sl(9, 12)]]
        cov = {S.key(s): n for s, n in S.slot_counts([a] + others)}
        self.assertEqual(S.choose_for_city(a, self.pref, cov), sl(9, 12))

    def test_summary(self):
        self.assertIn("— 2", S.summary([sl(9, 10), sl(9, 10), sl(11, 10)]))


if __name__ == "__main__":
    unittest.main()
