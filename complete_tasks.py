#!/usr/bin/env python3
"""
Отметить «хвосты» выполненными в Google Tasks.
Запуск на маке (где есть сеть и token.pickle):
    cd ~/Desktop/alta-assistant && python3 complete_tasks.py

Скрипт показывает все активные задачи, находит совпадения по ключевым словам
и помечает их выполненными. Неоднозначные/ненайденные — не трогает, просто сообщает.
"""

import os
import re
import pickle

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

TOKEN_FILE = "token.pickle"

# Цель: (человекочитаемое имя, [обязательные ключевые слова в нижнем регистре])
TARGETS = [
    ("Владу отправить концепт",                 ["влад", "концепт"]),
    ("Оплатить Hexlet",                          ["hexlet"]),
    ("Подготовить материал для Рузаны",          ["рузан"]),
    ("Бизнес-модели: свести 10 штук",            ["бизнес", "модел"]),
    ("Закрыть хвосты с партнёрами",              ["хвост", "партн"]),
    ("Сделать таблицу рынка",                    ["табл", "рынк"]),
    ("Договориться с Настасьей по Рыжанок",      ["рыжанок"]),
]


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def get_service():
    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)
    if creds and creds.refresh_token and (not creds.valid or creds.expired or creds.expiry is None):
        creds.refresh(Request())
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("tasks", "v1", credentials=creds)


def main():
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit("Нет token.pickle рядом со скриптом. Запусти из папки alta-assistant.")
    svc = get_service()

    items = svc.tasks().list(tasklist="@default", showCompleted=False, maxResults=100).execute().get("items", [])
    print(f"Активных задач в Google Tasks: {len(items)}\n")
    for t in items:
        print("  •", t.get("title"))
    print()

    completed, not_found, ambiguous = [], [], []
    for name, keys in TARGETS:
        matches = [t for t in items
                   if all(k in norm(t.get("title")) for k in keys)
                   and t.get("status") != "completed"]
        if len(matches) == 1:
            t = matches[0]
            t["status"] = "completed"
            svc.tasks().update(tasklist="@default", taskId=t["id"], body=t).execute()
            completed.append(t.get("title"))
        elif len(matches) == 0:
            not_found.append(name)
        else:
            ambiguous.append((name, [m.get("title") for m in matches]))

    print("=" * 50)
    print(f"✅ Отмечено выполненными ({len(completed)}):")
    for c in completed:
        print("   ✓", c)
    if not_found:
        print(f"\n⚠️ Не найдено в Google Tasks ({len(not_found)}) — возможно их там нет:")
        for n in not_found:
            print("   –", n)
    if ambiguous:
        print(f"\n❓ Несколько совпадений (не трогал, реши вручную):")
        for n, ms in ambiguous:
            print(f"   – {n}: {ms}")
    print("=" * 50)


if __name__ == "__main__":
    main()
