"""Печатает описание выпуска для текущей версии (из version.py) или CHANGELOG целиком: --all."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from version import CHANGES, VERSION  # noqa: E402


def block(v, d, items):
    return f"## {v} — {'.'.join(reversed(d.split('-')))}\n\n" + "\n".join(f"- {i}" for i in items) + "\n"


def out(text):
    """С --out ФАЙЛ пишем в файл в UTF-8 (консоль Windows может не уметь кириллицу)."""
    if "--out" in sys.argv:
        with open(sys.argv[sys.argv.index("--out") + 1], "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)


if "--all" in sys.argv:
    out("# История изменений\n\n" + "\n".join(block(*c) for c in CHANGES))
elif "--check-tag" in sys.argv:
    tag = sys.argv[-1].lstrip("vV")
    if tag != VERSION:
        sys.exit(f"Метка v{tag} не совпадает с VERSION = {VERSION} в version.py")
    print(f"Версия {VERSION} совпадает с меткой")
else:
    v = next(c for c in CHANGES if c[0] == VERSION)
    out("\n".join(f"- {i}" for i in v[2]) + "\n\nСкачайте архив ниже, распакуйте и запустите «Поставки Ozon.exe». "
        "Настройки и ключи сохранятся.")
