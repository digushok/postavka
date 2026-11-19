import os
import tempfile
import unittest

os.environ["OZON_SUPPLIES_DATA"] = tempfile.mkdtemp()
import storage as ST  # noqa: E402


class CurrentShop(unittest.TestCase):
    def tearDown(self):
        ST.set_current(None)

    def test_browser_choice_beats_default(self):
        a = ST.load_profiles()["profiles"][0]["id"]
        b = ST.new_profile("Второй")["id"]          # становится выбором по умолчанию
        ST.set_current(a)
        self.assertEqual(ST.get_profile()["id"], a)
        ST.set_current(None)
        self.assertEqual(ST.get_profile()["id"], b)

    def test_unknown_shop_falls_back_to_default(self):
        ST.set_current("нет-такого")
        self.assertEqual(ST.get_profile()["id"], ST.load_profiles()["active"])


if __name__ == "__main__":
    unittest.main()
