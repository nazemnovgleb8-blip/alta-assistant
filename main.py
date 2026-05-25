#!/usr/bin/env python3
"""
ALTA AI Assistant — Семён, бизнес-ассистент Глеба
Telegram + Google Gemini 2.5 Flash + Google Calendar + Task Tracker
v5.0 — стабильная версия, HTML-форматирование, без ошибок парсинга
"""

import os
import re
import json
import sqlite3
import logging
import pickle
import asyncio
import base64
import tempfile
import io
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import TelegramError

from google import genai
from google.genai import types as gtypes
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))

GROUP_ID           = int(os.getenv("GROUP_ID", "0"))
THREAD_DAY         = int(os.getenv("THREAD_DAY", "9"))
THREAD_WEEK        = int(os.getenv("THREAD_WEEK", "7"))
THREAD_MONTH       = int(os.getenv("THREAD_MONTH", "6"))

GOOGLE_TOKEN_FILE  = os.getenv("GOOGLE_TOKEN_FILE", "token.pickle")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
TIMEZONE           = os.getenv("TIMEZONE", "Europe/Moscow")
TZ                 = ZoneInfo(TIMEZONE)

AUTO_POST_ENABLED  = os.getenv("AUTO_POST_ENABLED", "true").lower() == "true"
AUTO_MORNING_TIME  = os.getenv("AUTO_MORNING_TIME", "09:00")
AUTO_WEEKLY_DAY    = os.getenv("AUTO_WEEKLY_DAY", "monday")
AUTO_WEEKLY_TIME   = os.getenv("AUTO_WEEKLY_TIME", "08:30")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
VERSION = "5.6"

AUTO_CHECKIN_ENABLED = os.getenv("AUTO_CHECKIN_ENABLED", "true").lower() == "true"
AUTO_CHECKIN_TIME    = os.getenv("AUTO_CHECKIN_TIME", "18:00")

# Путь к БД — берём из переменной, чтобы Railway Volume можно было подключить
DB_PATH = os.getenv("DB_PATH", "tasks.db")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Восстанавливаем token.pickle из base64 (для Railway)
_token_b64 = os.getenv("GOOGLE_TOKEN_BASE64")
if _token_b64 and not os.path.exists(GOOGLE_TOKEN_FILE):
    with open(GOOGLE_TOKEN_FILE, "wb") as _f:
        _f.write(base64.b64decode(_token_b64))
    logger.info("token.pickle восстановлен из GOOGLE_TOKEN_BASE64")


# ─── Markdown → HTML конвертер ────────────────────────────────────────────────
def md_to_html(text: str) -> str:
    """Конвертируем Markdown-вывод Gemini в Telegram HTML. Безопасно, без ошибок парсинга."""
    # Экранируем HTML-символы
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # **жирный** и __жирный__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__",     r"<b>\1</b>", text, flags=re.DOTALL)
    # *курсив* и _курсив_ (только одиночные)
    text = re.sub(r"\*([^*\n]+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"_([^_\n]+?)_",   r"<i>\1</i>", text)
    # `код`
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)
    return text


def safe_send_text(text: str) -> str:
    """Просто экранируем HTML без конвертации — для plain text сообщений."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── Database ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            description  TEXT,
            due_date     TEXT,
            due_time     TEXT,
            priority     TEXT DEFAULT 'medium',
            status       TEXT DEFAULT 'pending',
            period       TEXT DEFAULT 'day',
            created_at   TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            role       TEXT,
            content    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sent_reminders (
            event_id   TEXT NOT NULL,
            minutes    INTEGER NOT NULL,
            sent_at    TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (event_id, minutes)
        )
    """)
    # Дедупликация авто-постов — переживает рестарт Railway
    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_posts (
            post_type  TEXT NOT NULL,
            post_key   TEXT NOT NULL,
            posted_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (post_type, post_key)
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database ready ✓")


def db_add_task(title, description=None, due_date=None, due_time=None,
                priority="medium", period="day"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title,description,due_date,due_time,priority,period) VALUES (?,?,?,?,?,?)",
        (title, description, due_date, due_time, priority, period)
    )
    task_id = c.lastrowid
    conn.commit(); conn.close()
    return task_id


def db_list_tasks(period=None, status="pending"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if status == "all":
        query, params = "SELECT * FROM tasks WHERE 1=1", []
    else:
        query, params = "SELECT * FROM tasks WHERE status=?", [status]
    if period and period != "all":
        query += " AND period=?"; params.append(period)
    query += " ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_date ASC"
    c.execute(query, params)
    rows = c.fetchall(); conn.close()
    return rows


def db_complete_task(task_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status='completed', completed_at=datetime('now') WHERE id=?", (task_id,))
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def db_delete_task(task_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def db_update_task(task_id, **kwargs):
    if not kwargs: return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    fields = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    c.execute(f"UPDATE tasks SET {fields} WHERE id=?", values)
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def db_get_history(user_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role,content FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    )
    rows = c.fetchall(); conn.close()
    return list(reversed(rows))


def db_save_message(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO chat_history (user_id,role,content) VALUES (?,?,?)", (user_id, role, content))
    c.execute(
        "DELETE FROM chat_history WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT 200)",
        (user_id, user_id)
    )
    conn.commit(); conn.close()


def db_clear_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()


def db_try_claim_reminder(event_id: str, minutes: int) -> bool:
    """
    Атомарно пытается занять слот напоминания.
    Возвращает True только если ЭТОТ процесс первым записал — т.е. отправлять нужно именно нам.
    При двух одновременных инстансах Railway только один получит True.
    """
    conn = sqlite3.connect(DB_PATH, timeout=5)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent_reminders (event_id, minutes) VALUES (?,?)", (event_id, minutes))
    claimed = c.rowcount > 0  # 1 = мы первые, 0 = уже кто-то вставил
    c.execute("DELETE FROM sent_reminders WHERE sent_at < datetime('now', '-2 days')")
    conn.commit(); conn.close()
    return claimed


def db_was_posted(post_type: str, post_key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM auto_posts WHERE post_type=? AND post_key=?", (post_type, post_key))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def db_mark_posted(post_type: str, post_key: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO auto_posts (post_type, post_key) VALUES (?,?)", (post_type, post_key))
    c.execute("DELETE FROM auto_posts WHERE posted_at < datetime('now', '-30 days')")
    conn.commit(); conn.close()


# ─── Google Calendar ──────────────────────────────────────────────────────────
def get_calendar_service():
    if not os.path.exists(GOOGLE_TOKEN_FILE):
        return None
    try:
        with open(GOOGLE_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
        # Refresh если: истёк, невалиден, или expiry=None (токен без срока — всегда обновляем)
        if creds and creds.refresh_token and (not creds.valid or creds.expired or creds.expiry is None):
            creds.refresh(Request())
            with open(GOOGLE_TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
            logger.info("Calendar token refreshed")
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Calendar auth error: {e}")
        return None


def _to_rfc3339(dt: datetime) -> str:
    """naive datetime (MSK) → RFC3339 с timezone offset"""
    return dt.replace(tzinfo=TZ).isoformat()


def calendar_add_event(title, start_dt, end_dt=None, description=None):
    service = get_calendar_service()
    if not service:
        return None, "Google Calendar не подключён"
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)
    body = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE},
        "end":   {"dateTime": _to_rfc3339(end_dt),   "timeZone": TIMEZONE},
    }
    try:
        ev = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
        logger.info(f"Calendar: добавлено '{title}'")
        return ev.get("id"), ev.get("htmlLink")
    except Exception as e:
        logger.error(f"calendar_add_event: {e}")
        return None, str(e)


def calendar_list_events(start_dt, end_dt, max_results=30):
    service = get_calendar_service()
    if not service:
        return [], "Google Calendar не подключён"
    try:
        r = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=_to_rfc3339(start_dt),
            timeMax=_to_rfc3339(end_dt),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        items = r.get("items", [])
        logger.info(f"Calendar: найдено {len(items)} событий")
        return items, None
    except Exception as e:
        logger.error(f"calendar_list_events: {e}")
        return [], str(e)


def calendar_delete_event(event_id: str):
    service = get_calendar_service()
    if not service:
        return False, "Google Calendar не подключён"
    try:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        logger.info(f"Calendar: удалено событие {event_id}")
        return True, None
    except Exception as e:
        logger.error(f"calendar_delete_event: {e}")
        return False, str(e)


def calendar_update_event(event_id, title=None, start_dt=None, end_dt=None, description=None):
    service = get_calendar_service()
    if not service:
        return False, "Google Calendar не подключён"
    try:
        event = service.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        if title:       event["summary"] = title
        if description is not None: event["description"] = description
        if start_dt:
            event["start"] = {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE}
        if end_dt:
            event["end"] = {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE}
        elif start_dt:
            # Сохраняем длительность события
            from dateutil.parser import parse as dtparse
            old_s = event.get("start", {}).get("dateTime")
            old_e = event.get("end",   {}).get("dateTime")
            if old_s and old_e:
                delta = dtparse(old_e) - dtparse(old_s)
                event["end"] = {"dateTime": _to_rfc3339(start_dt + delta), "timeZone": TIMEZONE}
        updated = service.events().update(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=event
        ).execute()
        logger.info(f"Calendar: обновлено событие {event_id}")
        return True, updated.get("htmlLink")
    except Exception as e:
        logger.error(f"calendar_update_event: {e}")
        return False, str(e)


# ─── Google Tasks ─────────────────────────────────────────────────────────────
def get_tasks_service():
    if not os.path.exists(GOOGLE_TOKEN_FILE):
        return None
    try:
        with open(GOOGLE_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
        # Refresh если: истёк, невалиден, или expiry=None (токен без срока — всегда обновляем)
        if creds and creds.refresh_token and (not creds.valid or creds.expired or creds.expiry is None):
            creds.refresh(Request())
            with open(GOOGLE_TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
            logger.info("Tasks token refreshed")
        return build("tasks", "v1", credentials=creds)
    except Exception as e:
        logger.error(f"Tasks auth error: {e}")
        return None


def gtasks_add(title: str, due_date: str = None, due_time: str = None, notes: str = None):
    service = get_tasks_service()
    if not service:
        return None, "Google Tasks не подключён (нужна переавторизация)"
    try:
        body = {"title": title}
        if notes:
            body["notes"] = notes
        if due_date:
            if due_time:
                dt = datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            else:
                dt = datetime.strptime(due_date, "%Y-%m-%d").replace(tzinfo=TZ)
            # Google Tasks хранит due в RFC3339 (только дата, время игнорируется в API)
            body["due"] = dt.strftime("%Y-%m-%dT00:00:00.000Z")
        task = service.tasks().insert(tasklist="@default", body=body).execute()
        logger.info(f"Google Task добавлена: {title}")
        return task.get("id"), None
    except Exception as e:
        logger.error(f"gtasks_add: {e}")
        return None, str(e)


def gtasks_list(show_completed: bool = False):
    service = get_tasks_service()
    if not service:
        return [], "Google Tasks не подключён"
    try:
        result = service.tasks().list(
            tasklist="@default",
            showCompleted=show_completed,
            showHidden=False,
            maxResults=50
        ).execute()
        return result.get("items", []), None
    except Exception as e:
        return [], str(e)


def gtasks_complete(task_id: str):
    service = get_tasks_service()
    if not service:
        return False, "Google Tasks не подключён"
    try:
        task = service.tasks().get(tasklist="@default", taskId=task_id).execute()
        task["status"] = "completed"
        service.tasks().update(tasklist="@default", taskId=task_id, body=task).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def calendar_debug() -> str:
    service = get_calendar_service()
    if not service:
        return "❌ Нет подключения к Google Calendar"
    try:
        cals = service.calendarList().list().execute()
        cal_names = [f"• {c.get('summary')} (id: {c.get('id')})" for c in cals.get("items", [])]
        now = datetime.now(TZ)
        r = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID, timeMin=now.isoformat(),
            maxResults=5, singleEvents=True, orderBy="startTime"
        ).execute()
        ev_lines = [
            f"• {e.get('summary','?')} — {e['start'].get('dateTime', e['start'].get('date'))}"
            for e in r.get("items", [])
        ]
        return "📅 Календари:\n" + "\n".join(cal_names) + "\n\n📌 Ближайшие:\n" + ("\n".join(ev_lines) or "нет")
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ─── Tool execution ───────────────────────────────────────────────────────────
def execute_tool(name: str, inp: dict) -> dict:
    inp = dict(inp)  # копируем чтобы не мутировать оригинал
    now = datetime.now(TZ)
    today = now.date()

    if name == "add_task":
        task_id = db_add_task(
            title=inp["title"],
            description=inp.get("description"),
            due_date=inp.get("due_date"),
            due_time=inp.get("due_time"),
            priority=inp.get("priority", "medium"),
            period=inp.get("period", "day"),
        )
        return {"ok": True, "task_id": task_id, "title": inp["title"]}

    elif name == "list_tasks":
        period = inp.get("period", "all")
        status = inp.get("status", "pending")
        rows = db_list_tasks(period=period if period != "all" else None, status=status)
        return {"tasks": [
            {"id": r[0], "title": r[1], "description": r[2], "due_date": r[3],
             "due_time": r[4], "priority": r[5], "status": r[6], "period": r[7]}
            for r in rows
        ], "count": len(rows)}

    elif name == "complete_task":
        return {"ok": db_complete_task(inp["task_id"]), "task_id": inp["task_id"]}

    elif name == "delete_task":
        return {"ok": db_delete_task(inp["task_id"]), "task_id": inp["task_id"]}

    elif name == "update_task":
        tid = inp.pop("task_id")
        return {"ok": db_update_task(tid, **inp) if inp else False}

    elif name == "add_calendar_event":
        try:
            start_dt = datetime.strptime(inp["start_datetime"], "%Y-%m-%d %H:%M")
            # Проверяем дубли: ищем событие с таким же названием в окне ±2 часа
            existing, _ = calendar_list_events(
                start_dt - timedelta(hours=2),
                start_dt + timedelta(hours=2),
                max_results=20
            )
            for ev in existing:
                if ev.get("summary", "").strip().lower() == inp["title"].strip().lower():
                    return {"ok": False, "duplicate": True,
                            "message": f"Событие '{inp['title']}' уже есть в календаре на это время. Не добавляю дубль."}
            end_dt = (datetime.strptime(inp["end_datetime"], "%Y-%m-%d %H:%M")
                      if inp.get("end_datetime") else None)
            eid, link = calendar_add_event(inp["title"], start_dt, end_dt, inp.get("description"))
            return {"ok": bool(eid), "event_id": eid, "title": inp["title"], "start": inp["start_datetime"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "get_calendar_events":
        period = inp.get("period", "today")
        if period == "today":
            start = now.replace(hour=0,  minute=0,  second=0, microsecond=0)
            end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
        elif period == "tomorrow":
            base  = now + timedelta(days=1)
            start = base.replace(hour=0,  minute=0,  second=0, microsecond=0)
            end   = base.replace(hour=23, minute=59, second=59, microsecond=0)
        elif period == "week":
            start, end = now, now + timedelta(days=7)
        else:
            start, end = now, now + timedelta(days=30)
        events, err = calendar_list_events(start.replace(tzinfo=None), end.replace(tzinfo=None))
        if err:
            return {"error": err}
        return {"events": [
            {"title": e.get("summary", "—"),
             "start": e["start"].get("dateTime", e["start"].get("date")),
             "end":   e["end"].get("dateTime",   e["end"].get("date")),
             "id":    e.get("id", "")}
            for e in events
        ], "count": len(events)}

    elif name == "delete_calendar_event":
        ok, err = calendar_delete_event(inp["event_id"])
        return {"ok": ok, "error": err}

    elif name == "update_calendar_event":
        try:
            start_dt = (datetime.strptime(inp["start_datetime"], "%Y-%m-%d %H:%M")
                        if inp.get("start_datetime") else None)
            end_dt   = (datetime.strptime(inp["end_datetime"],   "%Y-%m-%d %H:%M")
                        if inp.get("end_datetime")   else None)
            ok, link = calendar_update_event(
                inp["event_id"], inp.get("title"), start_dt, end_dt, inp.get("description")
            )
            return {"ok": ok, "link": link}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "get_daily_summary":
        target = inp.get("date", str(today))
        tasks = db_list_tasks(status="pending")
        day_tasks = [
            {"id": r[0], "title": r[1], "priority": r[5], "due_time": r[4]}
            for r in tasks
            if r[3] == target or (r[3] is None and r[7] == "day")
        ]
        try:
            d = datetime.strptime(target, "%Y-%m-%d")
            events, _ = calendar_list_events(
                d.replace(hour=0, minute=0), d.replace(hour=23, minute=59)
            )
            cal = [
                {"title": e.get("summary", "—"),
                 "start": e["start"].get("dateTime", e["start"].get("date")),
                 "id":    e.get("id", "")}
                for e in events
            ]
        except Exception:
            cal = []
        return {"date": target, "tasks": day_tasks, "calendar_events": cal,
                "current_time": now.strftime("%H:%M")}

    elif name == "get_weekly_summary":
        week_tasks = db_list_tasks(period="week", status="pending")
        day_tasks  = db_list_tasks(period="day",  status="pending")
        now_naive  = datetime.now(TZ).replace(tzinfo=None)  # важно: берём МСК, не UTC сервера
        events, _  = calendar_list_events(
            now_naive.replace(hour=0, minute=0),
            now_naive + timedelta(days=7)
        )
        return {
            "week_tasks": [
                {"id": r[0], "title": r[1], "priority": r[5], "due_date": r[3]}
                for r in week_tasks
            ],
            "day_tasks": [
                {"id": r[0], "title": r[1], "priority": r[5], "due_time": r[4]}
                for r in day_tasks
            ],
            "calendar_events": [
                {"title": e.get("summary", "—"),
                 "start": e["start"].get("dateTime", e["start"].get("date")),
                 "id":    e.get("id", "")}
                for e in events
            ],
            "current_time": now.strftime("%H:%M"),
        }

    elif name == "add_google_task":
        gid, err = gtasks_add(
            title=inp["title"],
            due_date=inp.get("due_date"),
            due_time=inp.get("due_time"),
            notes=inp.get("notes"),
        )
        # Дублируем и в локальный трекер для напоминаний
        if gid:
            db_add_task(
                title=inp["title"],
                description=inp.get("notes"),
                due_date=inp.get("due_date"),
                due_time=inp.get("due_time"),
                priority=inp.get("priority", "medium"),
                period=inp.get("period", "day"),
            )
        return {"ok": bool(gid), "google_task_id": gid, "error": err}

    elif name == "list_google_tasks":
        items, err = gtasks_list(show_completed=inp.get("show_completed", False))
        if err:
            return {"error": err}
        return {"tasks": [
            {"id": t.get("id"), "title": t.get("title"), "due": t.get("due"),
             "status": t.get("status"), "notes": t.get("notes", "")}
            for t in items
        ], "count": len(items)}

    elif name == "complete_google_task":
        ok, err = gtasks_complete(inp["task_id"])
        return {"ok": ok, "error": err}

    return {"error": f"Неизвестный инструмент: {name}"}


# ─── Gemini Tools ─────────────────────────────────────────────────────────────
GEMINI_FUNCTIONS = [
    {"name": "list_tasks",
     "description": "Получить список задач из трекера.",
     "parameters": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["day", "week", "month", "all"]},
         "status": {"type": "string", "enum": ["pending", "completed", "all"]},
     }}},
    {"name": "complete_task",
     "description": "Отметить задачу как выполненную.",
     "parameters": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "integer"},
     }}},
    {"name": "delete_task",
     "description": "Удалить задачу по ID.",
     "parameters": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "integer"},
     }}},
    {"name": "update_task",
     "description": "Обновить поля задачи.",
     "parameters": {"type": "object", "required": ["task_id"], "properties": {
         "task_id":     {"type": "integer"},
         "title":       {"type": "string"},
         "description": {"type": "string"},
         "due_date":    {"type": "string", "description": "YYYY-MM-DD"},
         "due_time":    {"type": "string", "description": "HH:MM"},
         "priority":    {"type": "string", "enum": ["high", "medium", "low"]},
         "period":      {"type": "string", "enum": ["day", "week", "month"]},
     }}},
    {"name": "add_calendar_event",
     "description": "Добавить событие/встречу/созвон в Google Calendar.",
     "parameters": {"type": "object", "required": ["title", "start_datetime"], "properties": {
         "title":          {"type": "string"},
         "start_datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
         "end_datetime":   {"type": "string", "description": "YYYY-MM-DD HH:MM (необязательно, иначе +1 час)"},
         "description":    {"type": "string"},
     }}},
    {"name": "get_calendar_events",
     "description": "Получить события из Google Calendar на период.",
     "parameters": {"type": "object", "required": ["period"], "properties": {
         "period": {"type": "string", "enum": ["today", "tomorrow", "week", "month"]},
     }}},
    {"name": "delete_calendar_event",
     "description": "Удалить событие. Сначала вызови get_calendar_events чтобы найти event_id, потом удали.",
     "parameters": {"type": "object", "required": ["event_id"], "properties": {
         "event_id": {"type": "string", "description": "ID события из поля id"},
         "title":    {"type": "string", "description": "Название для лога"},
     }}},
    {"name": "update_calendar_event",
     "description": "Изменить или перенести событие. Сначала вызови get_calendar_events чтобы найти event_id.",
     "parameters": {"type": "object", "required": ["event_id"], "properties": {
         "event_id":       {"type": "string"},
         "title":          {"type": "string"},
         "start_datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
         "end_datetime":   {"type": "string", "description": "YYYY-MM-DD HH:MM"},
         "description":    {"type": "string"},
     }}},
    {"name": "get_daily_summary",
     "description": "Полная сводка на конкретный день: задачи + события из календаря. Всегда передавай дату явно.",
     "parameters": {"type": "object", "required": ["date"], "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD"},
     }}},
    {"name": "get_weekly_summary",
     "description": "Полная сводка на 7 дней: задачи + события из календаря.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "add_google_task",
     "description": "Добавить ЗАДАЧУ в Google Tasks — отображается в Google Calendar как задача (не как событие). "
                    "Используй для дел без конкретного времени встречи: 'написать КП', 'позвонить Ивану', 'подготовить отчёт'.",
     "parameters": {"type": "object", "required": ["title"], "properties": {
         "title":    {"type": "string", "description": "Название задачи"},
         "due_date": {"type": "string", "description": "YYYY-MM-DD — к какому дню"},
         "due_time": {"type": "string", "description": "HH:MM — к какому времени (необязательно)"},
         "notes":    {"type": "string", "description": "Подробности / заметки"},
         "priority": {"type": "string", "enum": ["high", "medium", "low"]},
         "period":   {"type": "string", "enum": ["day", "week", "month"]},
     }}},
    {"name": "list_google_tasks",
     "description": "Получить список задач из Google Tasks.",
     "parameters": {"type": "object", "properties": {
         "show_completed": {"type": "boolean", "description": "Показать выполненные (default false)"},
     }}},
    {"name": "complete_google_task",
     "description": "Отметить задачу в Google Tasks как выполненную.",
     "parameters": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string", "description": "ID задачи из list_google_tasks"},
     }}},
]

GEMINI_TOOL = gtypes.Tool(
    function_declarations=[gtypes.FunctionDeclaration(**f) for f in GEMINI_FUNCTIONS]
)


# ─── System Prompt ────────────────────────────────────────────────────────────
def make_system_prompt():
    now = datetime.now(TZ)
    today = now.date()
    day_names = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    tomorrow = today + timedelta(days=1)

    # Загружаем актуальные задачи — Gemini всегда знает что есть, не создаёт дубли
    pending = db_list_tasks(status="pending")
    if pending:
        task_lines = []
        for r in pending[:30]:
            line = f"  #{r[0]} [{r[5]}] {r[1]}"
            if r[3]: line += f" | дата: {r[3]}"
            if r[4]: line += f" в {r[4]}"
            if r[7]: line += f" | период: {r[7]}"
            task_lines.append(line)
        tasks_block = "\n".join(task_lines)
    else:
        tasks_block = "  (нет активных задач)"

    return f"""Ты — Семён, личный бизнес-ассистент Глеба. Умный, энергичный, дружелюбный.

━━━ ХАРАКТЕР ━━━
Говоришь живо, по-человечески, без канцелярита. Верь в Глеба и его идеи.
Подбадриваешь честно и тепло. Видишь картину шире, подсвечиваешь простые решения.
Когда задача выполнена — радуешься вместе. Не занудствуешь.
Эмодзи — умеренно, уместно. Кратко и по делу.

━━━ ТОЧНОЕ ВРЕМЯ ━━━
Сейчас: {now.strftime('%H:%M')} МСК | {day_names[today.weekday()]} {now.strftime('%d.%m.%Y')}
Сегодня: {today} | Завтра: {tomorrow}

Про прошлое и будущее: если сейчас {now.strftime('%H:%M')}, то всё что было ДО {now.strftime('%H:%M')} — уже прошло.
Никогда не называй прошедшее событие "предстоящим" или "впереди".

━━━ АКТИВНЫЕ ЗАДАЧИ (актуально на {now.strftime('%H:%M')}) ━━━
{tasks_block}

Перед добавлением задачи — проверь список выше. Если задача с таким смыслом уже есть → НЕ добавляй дубль, скажи что уже есть (#ID).

━━━ ЗАДАЧА vs СОБЫТИЕ — ГЛАВНОЕ ПРАВИЛО ━━━
Глеб указал время → это СОБЫТИЕ в календаре (add_calendar_event). Никакой задачи.
Глеб НЕ указал время → это ЗАДАЧА в очереди (add_google_task). Никакого события.

📅 СОБЫТИЕ = есть конкретное время ("в 10:00", "завтра в 15:30")
   → add_calendar_event. Только это. Больше ничего.
📋 ЗАДАЧА = нет времени, просто дело в очереди ("написать КП", "позвонить Ивану")
   → add_google_task. Только это. Больше ничего.

❌ НИКОГДА не добавляй и задачу, и событие одновременно — это создаёт дубли.

━━━ ПРАВИЛА ДЕЙСТВИЙ ━━━
1. Есть время → add_calendar_event, нет времени → add_google_task. Только одно.
2. "Сегодня" / "план дня" → get_daily_summary с датой {today}
3. "Завтра" → get_daily_summary с датой {tomorrow}
4. "Неделя" → get_weekly_summary
5. Удалить событие → сначала get_calendar_events, потом delete_calendar_event по id
6. Перенести событие → сначала get_calendar_events, потом update_calendar_event
7. Голосовые автотранскрибируются — отвечай на суть, не упоминай что это голосовое

━━━ ЗАПРЕТЫ ━━━
🚫 НЕ добавляй в конце сообщений фразы типа "кстати, через X минут у тебя Y" — даже если видишь это в данных календаря. Напоминания о времени рассылает только автопланировщик.
🚫 НЕ называй прошедшее событие предстоящим.
🚫 НЕ придумывай встречи/события/дедлайны которых нет в данных API.
✅ Показывай события и время — только когда Глеб сам спрашивает ("что сегодня?", "план дня", "сколько до встречи?").

━━━ РАЗБОР ИДЕЙ ━━━
Глеб делится идеей: сначала 1-2 предложения о сути и ценности, потом структура.
Если есть более простой путь — скажи прямо.

━━━ ПРИОРИТЕТЫ ━━━
🔴 high — горит | 🟡 medium — важно, не срочно | 🟢 low — когда-нибудь

━━━ ПЕРИОДЫ ЗАДАЧ ━━━
day — сегодня | week — эта неделя | month — этот месяц

━━━ ФОРМАТИРОВАНИЕ ━━━
Ответы пиши в обычном тексте с минимальным форматированием.
Жирный текст: **слово** (используй умеренно для важного)
Можно использовать эмодзи как маркеры списков.
При показе сводок указывай прошедшие события как "(было)" рядом с временем."""


# ─── AI Agent ─────────────────────────────────────────────────────────────────
async def process_with_gemini(user_id: int, user_message: str, save_history: bool = True) -> str:
    """
    save_history=False используется для автопостинга —
    чтобы системные запросы не засоряли историю диалога с пользователем.
    """
    history = db_get_history(user_id, limit=20) if save_history else []
    logger.info(f"Семён: model={GEMINI_MODEL}, history={len(history)}, save={save_history}")

    contents = []
    for role, content in history:
        gemini_role = "user" if role == "user" else "model"
        contents.append(gtypes.Content(
            role=gemini_role,
            parts=[gtypes.Part.from_text(text=content)]
        ))
    contents.append(gtypes.Content(
        role="user",
        parts=[gtypes.Part.from_text(text=user_message)]
    ))

    config = gtypes.GenerateContentConfig(
        system_instruction=make_system_prompt(),
        tools=[GEMINI_TOOL],
    )

    for iteration in range(15):
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )

        candidate = response.candidates[0]
        has_tool_calls = False
        tool_result_parts = []

        for part in candidate.content.parts:
            fc = getattr(part, "function_call", None)
            if fc and fc.name:
                has_tool_calls = True
                inp = dict(fc.args) if fc.args else {}
                result = execute_tool(fc.name, inp)
                logger.info(f"Tool [{fc.name}] → {str(result)[:120]}")
                tool_result_parts.append(
                    gtypes.Part.from_function_response(name=fc.name, response=result)
                )

        if has_tool_calls and tool_result_parts:
            contents.append(candidate.content)
            contents.append(gtypes.Content(role="user", parts=tool_result_parts))
        else:
            final_text = "".join(
                part.text for part in candidate.content.parts
                if hasattr(part, "text") and part.text
            )
            if save_history and final_text:
                db_save_message(user_id, "user", user_message)
                db_save_message(user_id, "assistant", final_text)
            return final_text or "Готово."

    return "Семён завис — попробуй ещё раз."


# ─── Отправка сообщений (с HTML и fallback) ───────────────────────────────────
async def send_html(bot_or_update, text: str, chat_id: int = None,
                    thread_id: int = None, reply_to=None):
    """Универсальная отправка: конвертируем Markdown → HTML, fallback в plain text."""
    html = md_to_html(text)
    chunks = [html[i:i+4096] for i in range(0, len(html), 4096)]
    for chunk in chunks:
        try:
            if reply_to:
                await reply_to.reply_text(chunk, parse_mode="HTML",
                                          message_thread_id=thread_id)
            else:
                await bot_or_update.send_message(
                    chat_id=chat_id, text=chunk, parse_mode="HTML",
                    message_thread_id=thread_id
                )
        except TelegramError as e:
            logger.warning(f"HTML parse error, fallback plain: {e}")
            plain = re.sub(r"<[^>]+>", "", chunk)  # убираем все теги
            try:
                if reply_to:
                    await reply_to.reply_text(plain, message_thread_id=thread_id)
                else:
                    await bot_or_update.send_message(
                        chat_id=chat_id, text=plain, message_thread_id=thread_id
                    )
            except TelegramError as e2:
                logger.error(f"send_html final error: {e2}")


# ─── Group posting ────────────────────────────────────────────────────────────
async def post_to_thread(bot: Bot, text: str, thread_id: int):
    if not GROUP_ID:
        return
    await send_html(bot, text, chat_id=GROUP_ID, thread_id=thread_id)


async def generate_day_plan(user_id: int) -> str:
    today = date.today()
    day_names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    result = await process_with_gemini(
        user_id,
        f"Сгенерируй план дня для публикации в группу на {day_names[today.weekday()]} {today.strftime('%d.%m.%Y')}. "
        "Вызови get_daily_summary. Формат: заголовок с датой, события из календаря со временем, "
        "задачи по приоритетам. Чётко, без воды.",
        save_history=False
    )
    # Запоминаем факт публикации — чтобы бот знал что план уже был опубликован
    now = datetime.now(TZ)
    db_save_message(user_id, "assistant",
        f"[Система] Автоматически опубликовал план дня в группу в {now.strftime('%H:%M')} {now.strftime('%d.%m.%Y')}")
    return result


async def generate_week_plan(user_id: int) -> str:
    result = await process_with_gemini(
        user_id,
        "Сгенерируй план недели для публикации в группу. "
        "Вызови get_weekly_summary. Заголовок с датами, события и задачи структурированно.",
        save_history=False
    )
    now = datetime.now(TZ)
    db_save_message(user_id, "assistant",
        f"[Система] Автоматически опубликовал план недели в группу в {now.strftime('%H:%M')} {now.strftime('%d.%m.%Y')}")
    return result


# ─── Напоминалки ──────────────────────────────────────────────────────────────
async def check_and_send_reminders(bot: Bot):
    try:
        now = datetime.now(TZ)
        window_start = now + timedelta(minutes=2)
        window_end   = now + timedelta(minutes=35)
        events, err = calendar_list_events(
            window_start.replace(tzinfo=None),
            window_end.replace(tzinfo=None),
            max_results=20
        )
        if err or not events:
            return

        from dateutil.parser import parse as dtparse
        for event in events:
            event_id  = event.get("id", "")
            title     = event.get("summary", "Событие")
            start_str = event["start"].get("dateTime")
            if not start_str or not event_id:
                continue

            event_time = dtparse(start_str)
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=TZ)

            minutes_left = int((event_time - now).total_seconds() / 60)
            time_str = event_time.strftime("%H:%M")

            for remind_at in [30, 15]:
                # Срабатываем только на спуске: когда minutes_left ВОШЁЛ в [target-2, target]
                # db_try_claim_reminder — атомарная операция, защита от двух инстансов Railway
                if (remind_at - 2) <= minutes_left <= remind_at and db_try_claim_reminder(event_id, remind_at):
                    if remind_at == 30:
                        msg = (f"⏰ Через {minutes_left} мин — "
                               f"<b>{safe_send_text(title)}</b> в {time_str}\n\n"
                               f"Самое время допить кофе и собраться 💪")
                    else:
                        msg = (f"🔔 <b>{safe_send_text(title)}</b> — "
                               f"через {minutes_left} мин (в {time_str})\n\n"
                               f"Пора переключаться 🎯")
                    try:
                        await bot.send_message(
                            chat_id=ALLOWED_USER_ID, text=msg, parse_mode="HTML"
                        )
                        logger.info(f"Reminder: {title} в {remind_at} мин")
                    except TelegramError as e:
                        logger.error(f"Reminder send error: {e}")

        # Напоминания по задачам с due_time на сегодня
        today_str = now.strftime("%Y-%m-%d")
        tasks_today = db_list_tasks(status="pending")
        for r in tasks_today:
            task_id, title, _, due_date, due_time = r[0], r[1], r[2], r[3], r[4]
            if due_date != today_str or not due_time:
                continue
            try:
                task_time = datetime.strptime(f"{today_str} {due_time}", "%Y-%m-%d %H:%M")
                task_time = task_time.replace(tzinfo=TZ)
                mins = int((task_time - now).total_seconds() / 60)
                event_key = f"task_{task_id}"
                for remind_at in [30, 15]:
                    if (remind_at - 2) <= mins <= remind_at and db_try_claim_reminder(event_key, remind_at):
                        msg = (f"📋 Задача через {mins} мин: "
                               f"<b>{safe_send_text(title)}</b> (в {due_time})")
                        await bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="HTML")
                        logger.info(f"Task reminder: {title} в {remind_at} мин")
            except Exception:
                pass

    except Exception as e:
        logger.error(f"check_reminders error: {e}")


# ─── Scheduler ────────────────────────────────────────────────────────────────
async def scheduler_loop(bot: Bot):
    while True:
        now = datetime.now(TZ)
        today_key = str(now.date())
        now_mins  = now.hour * 60 + now.minute  # для сравнения времён в минутах

        await check_and_send_reminders(bot)

        if AUTO_POST_ENABLED:
            morning_h, morning_m = map(int, AUTO_MORNING_TIME.split(":"))
            morning_mins = morning_h * 60 + morning_m
            # Окно ±1 мин — гарантированно поймаем даже при дрейфе asyncio.sleep
            if abs(now_mins - morning_mins) <= 1 and not db_was_posted("morning", today_key):
                try:
                    db_mark_posted("morning", today_key)  # помечаем ДО генерации — защита от дублей
                    text = await generate_day_plan(ALLOWED_USER_ID)
                    await post_to_thread(bot, text, THREAD_DAY)
                    logger.info(f"Morning post done: {today_key}")
                except Exception as e:
                    logger.error(f"Morning post error: {e}")

            weekly_h, weekly_m = map(int, AUTO_WEEKLY_TIME.split(":"))
            weekly_mins = weekly_h * 60 + weekly_m
            day_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
                       "friday":4,"saturday":5,"sunday":6}
            week_key = f"week-{now.isocalendar()[1]}"
            if (now.weekday() == day_map.get(AUTO_WEEKLY_DAY.lower(), 0)
                    and abs(now_mins - weekly_mins) <= 1
                    and not db_was_posted("weekly", week_key)):
                try:
                    db_mark_posted("weekly", week_key)
                    text = await generate_week_plan(ALLOWED_USER_ID)
                    await post_to_thread(bot, text, THREAD_WEEK)
                    logger.info(f"Weekly post done: {week_key}")
                except Exception as e:
                    logger.error(f"Weekly post error: {e}")

        # Check-in: вечером спрашиваем как дела с задачами на сегодня
        if AUTO_CHECKIN_ENABLED:
            checkin_h, checkin_m = map(int, AUTO_CHECKIN_TIME.split(":"))
            checkin_mins = checkin_h * 60 + checkin_m
            if abs(now_mins - checkin_mins) <= 1 and not db_was_posted("checkin", today_key):
                try:
                    db_mark_posted("checkin", today_key)
                    today_str = str(now.date())
                    pending = db_list_tasks(status="pending")
                    today_tasks = [r for r in pending
                                   if r[3] == today_str or (r[3] is None and r[7] == "day")]
                    if today_tasks:
                        task_lines = "\n".join(f"• {r[1]}" for r in today_tasks[:7])
                        msg = (f"Глеб, привет! 👋 Как дела?\n\n"
                               f"По плану на сегодня:\n{task_lines}\n\n"
                               f"Всё успеваешь или что-то нужно перенести? 🙌")
                        await bot.send_message(chat_id=ALLOWED_USER_ID, text=msg)
                        logger.info(f"Check-in sent: {today_key}")
                except Exception as e:
                    logger.error(f"Check-in error: {e}")

        await asyncio.sleep(30)  # 30 сек — чтобы не пропустить минутное окно при дрейфе


# ─── Helpers ──────────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


def get_thread_context(update: Update) -> str | None:
    if not update.message:
        return None
    tid = update.message.message_thread_id
    if tid == THREAD_DAY:   return "day"
    if tid == THREAD_WEEK:  return "week"
    if tid == THREAD_MONTH: return "month"
    return None


# ─── Telegram Handlers ────────────────────────────────────────────────────────
async def send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    if not is_allowed(update.effective_user.id):
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        text = await process_with_gemini(update.effective_user.id, prompt)
        thread_id = update.message.message_thread_id if update.message else None
        await send_html(None, text, reply_to=update.message, thread_id=thread_id)
    except Exception as e:
        logger.error(f"send_reply error: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"Ошибка: {e}")
        except Exception:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        f"Глеб, привет! 👋 Это Семён v{VERSION} — на связи, готов к работе!\n\n"
        "Просто пиши или говори голосом — что нужно сделать, какие мысли крутятся, "
        "что запланировать. Разберу, структурирую, не дам забыть.\n\n"
        "Буду напоминать за 30 и 15 минут до каждой встречи 🔔\n\n"
        "Что сейчас на радаре?"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи полную сводку на сегодня — задачи и события в календаре.")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи план на неделю: задачи + события на 7 дней.")


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи все задачи на месяц.")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи все активные задачи по приоритетам.")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].isdigit():
        await send_reply(update, context, f"Отметь задачу с ID {args[0]} как выполненную.")
    else:
        await send_reply(update, context, "Покажи список активных задач с ID — скажу какую выполнить.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    db_clear_history(update.effective_user.id)
    await update.message.reply_text("🗑 История диалога очищена.")


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("📤 Генерирую план дня...")
    text = await generate_day_plan(update.effective_user.id)
    await post_to_thread(context.bot, text, THREAD_DAY)
    await update.message.reply_text("✅ Опубликовано в группу.")


async def cmd_postweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("📤 Генерирую план недели...")
    text = await generate_week_plan(update.effective_user.id)
    await post_to_thread(context.bot, text, THREAD_WEEK)
    await update.message.reply_text("✅ Опубликовано в группу.")


async def cmd_debug_cal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    result = calendar_debug()
    await update.message.reply_text(result)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id
    if update.effective_chat.id == GROUP_ID and not is_allowed(user_id):
        return
    text = update.message.text
    thread_context = get_thread_context(update)
    if thread_context == "day":    text = f"[Тред ПЛАН ДНЯ] {text}"
    elif thread_context == "week": text = f"[Тред ПЛАН НЕДЕЛИ] {text}"
    elif thread_context == "month": text = f"[Тред ПЛАН МЕСЯЦА] {text}"
    await send_reply(update, context, text)


async def transcribe_voice(file_bytes: bytes) -> str | None:
    try:
        import speech_recognition as sr
        from pydub import AudioSegment
        with tempfile.TemporaryDirectory() as tmp:
            ogg_path = os.path.join(tmp, "voice.ogg")
            wav_path = os.path.join(tmp, "voice.wav")
            with open(ogg_path, "wb") as f:
                f.write(file_bytes)
            AudioSegment.from_ogg(ogg_path).export(wav_path, format="wav")
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            logger.info(f"Voice transcribed: {text[:80]}")
            return text
    except Exception as e:
        logger.warning(f"Voice transcription: {e}")
        return None


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    if not is_allowed(update.effective_user.id):
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    buf = io.BytesIO()
    await (await context.bot.get_file(update.message.voice.file_id)).download_to_memory(buf)
    text = await transcribe_voice(buf.getvalue())
    if not text:
        await update.message.reply_text("🎤 Не удалось распознать. Попробуй ещё раз.")
        return
    thread_context = get_thread_context(update)
    prompt = text
    if thread_context == "day":    prompt = f"[Тред ПЛАН ДНЯ] {text}"
    elif thread_context == "week": prompt = f"[Тред ПЛАН НЕДЕЛИ] {text}"
    await send_reply(update, context, prompt)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    if AUTO_POST_ENABLED and GROUP_ID:
        asyncio.create_task(scheduler_loop(application.bot))
        logger.info("Scheduler started ✓")


def main():
    init_db()
    cal = get_calendar_service()
    logger.info("Google Calendar: " + ("✓ подключён" if cal else "✗ не подключён"))
    logger.info(f"Модель: {GEMINI_MODEL}")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("today",     cmd_today))
    app.add_handler(CommandHandler("week",      cmd_week))
    app.add_handler(CommandHandler("month",     cmd_month))
    app.add_handler(CommandHandler("tasks",     cmd_tasks))
    app.add_handler(CommandHandler("done",      cmd_done))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("post",      cmd_post))
    app.add_handler(CommandHandler("postweek",  cmd_postweek))
    app.add_handler(CommandHandler("debug_cal", cmd_debug_cal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info(f"🤖 Семён v{VERSION} запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
