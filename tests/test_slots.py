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
    pref = {"date": "2026-11-09", "time": "10:00"}      # хочу 9-е, 10:00

    def test_same_day_other_hour(self):
        # 10:00 уже занято, на 9-е есть 12:00 — берём его, время не так важно, как день
        self.assertEqual(S.choose_for_city([sl(9, 12), sl(10, 10)], self.pref), sl(9, 12))
        self.assertEqual(S.choose_for_city([sl(9, 10), sl(9, 12)], self.pref), sl(9, 10))

    def test_nearest_day_any_direction(self):
        self.assertEqual(S.choose_for_city([sl(8, 10), sl(11, 10)], self.pref), sl(8, 10))   # 8-е ближе 11-го
        self.assertEqual(S.choose_for_city([sl(8, 10), sl(10, 10)], self.pref), sl(10, 10))  # поровну — позже

    def test_most_shared_on_equal(self):
        a = [sl(9, 7), sl(9, 13)]                       # обе на 3 часа от 10:00
        cov = {S.key(sl(9, 13)): 5, S.key(sl(9, 7)): 1}
        self.assertEqual(S.choose_for_city(a, self.pref, cov), sl(9, 13))

    def test_old_batches_with_range(self):
        old = {"date_from": "2026-11-09", "date_to": "2026-11-11", "time_from": "10:00", "time_to": "18:00"}
        self.assertEqual(S.choose_for_city([sl(9, 12), sl(11, 10)], old), sl(9, 12))

    def test_summary(self):
        self.assertIn("— 2", S.summary([sl(9, 10), sl(9, 10), sl(11, 10)]))


if __name__ == "__main__":
    unittest.main()
