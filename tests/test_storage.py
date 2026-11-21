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




class BatchTitle(unittest.TestCase):
    def test_title_survives_batch_save(self):
        pid = ST.load_profiles()["profiles"][0]["id"]
        ST.save_batch(pid, {"id": "b1", "title": "", "shipments": []})
        ST.rename_batch(pid, "b1", "Карты памяти")
        ST.save_batch(pid, {"id": "b1", "title": "", "shipments": []})   # создание поставок сохранило старую копию
        self.assertEqual(ST.load_batch(pid, "b1")["title"], "Карты памяти")


if __name__ == "__main__":
    unittest.main()
