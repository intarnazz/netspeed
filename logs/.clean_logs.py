#!/usr/bin/env python3
"""Очистка: удаляет всё, что лежит рядом с этим скриптом (кроме файлов, начинающихся с точки).

Запуск:  python .clean_logs.py          (спросит подтверждение)
         python .clean_logs.py --yes    (без подтверждения)
"""
import argparse
import shutil
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent  # папка, в которой лежит этот скрипт


def size_of(path):
    try:
        if path.is_dir() and not path.is_symlink():
            return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        return path.lstat().st_size
    except OSError:
        return 0


def fmt_size(num):
    value = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1000 or unit == "ГБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.2f} {unit}"
        value /= 1000


def main():
    parser = argparse.ArgumentParser(description="Очистка папки логов.")
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    args = parser.parse_args()

    items = [p for p in sorted(TARGET.iterdir()) if not p.name.startswith(".")]
    if not items:
        print("Папка логов уже пуста.")
        return 0

    total = sum(size_of(p) for p in items)
    print(f"Папка: {TARGET}")
    print(f"К удалению: элементов {len(items)}, общий размер {fmt_size(total)}")
    if not args.yes:
        try:
            answer = input("Удалить? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено, ничего не удалено.")
            return 1

    errors = 0
    for item in items:
        try:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError as exc:
            errors += 1
            print(f"Не удалось удалить {item.name}: {exc}")
    print(f"Готово. Удалено: {len(items) - errors}, ошибок: {errors}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
