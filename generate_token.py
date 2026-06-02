#!/usr/bin/env python3
"""
Генерация правильного token.pickle для Семёна (Google Calendar + Tasks).
Создаёт токен СО ВСЕМИ полями для обновления (refresh_token, client_id,
client_secret, token_uri) — это лечит ошибку
'credentials do not contain the necessary fields need to refresh the access token'.

КАК ЗАПУСТИТЬ (на маке, где есть браузер):
  1. Установи зависимости (один раз):
       pip3 install google-auth-oauthlib google-api-python-client --break-system-packages
  2. Положи рядом client_secret.json (из Google Cloud Console → Credentials →
     твой OAuth 2.0 Client ID → кнопка ⬇ Download JSON).
     ИЛИ задай переменные окружения GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET.
  3. Запусти:
       python3 generate_token.py
  4. Откроется браузер — войди в нужный Google-аккаунт и разреши доступ.
  5. Скрипт сохранит token.pickle и напечатает строку GOOGLE_TOKEN_BASE64 —
     скопируй её в переменную окружения GOOGLE_TOKEN_BASE64 в Railway (alta-assistant).
"""

import os
import json
import base64
import pickle

from google_auth_oauthlib.flow import InstalledAppFlow

# Доступ на чтение И запись: события календаря + задачи
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]

CLIENT_SECRET_FILE = "client_secret.json"


def build_flow():
    if os.path.exists(CLIENT_SECRET_FILE):
        print(f"Использую {CLIENT_SECRET_FILE}")
        return InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)

    cid = os.getenv("GOOGLE_CLIENT_ID")
    csec = os.getenv("GOOGLE_CLIENT_SECRET")
    if cid and csec:
        print("Использую GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET из окружения")
        client_config = {
            "installed": {
                "client_id": cid,
                "client_secret": csec,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        return InstalledAppFlow.from_client_config(client_config, SCOPES)

    raise SystemExit(
        "Нет client_secret.json и не заданы GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.\n"
        "Скачай client_secret.json из Google Cloud Console или задай переменные окружения."
    )


def main():
    flow = build_flow()
    # access_type=offline + prompt=consent — ОБЯЗАТЕЛЬНО, чтобы Google выдал refresh_token
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    with open("token.pickle", "wb") as f:
        pickle.dump(creds, f)

    print("\n" + "=" * 60)
    print("token.pickle сохранён ✓")
    print("refresh_token присутствует:", bool(creds.refresh_token))
    print("client_id присутствует:    ", bool(creds.client_id))
    print("client_secret присутствует:", bool(creds.client_secret))
    print("scopes:", creds.scopes)
    if not creds.refresh_token:
        print("\n⚠️  refresh_token НЕ получен! Зайди на https://myaccount.google.com/permissions, "
              "удали доступ приложения и запусти скрипт ещё раз.")
    print("=" * 60)

    b64 = base64.b64encode(open("token.pickle", "rb").read()).decode()
    print("\nСкопируй это в переменную GOOGLE_TOKEN_BASE64 в Railway (alta-assistant):\n")
    print(b64)
    print()


if __name__ == "__main__":
    main()
