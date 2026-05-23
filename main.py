#!/usr/bin/env python3
"""
ALTA AI Assistant — Бизнес-ассистент Глеба
Telegram + Google Gemini (БЕСПЛАТНО) + Google Calendar + Task Tracker
v4.0 — Gemini 2.0 Flash, бесплатная модель, умная
"""

import os
import json
import sqlite3
import logging
import pickle
import asyncio
import base64
import tempfile
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import TelegramError

import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
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

GEMINI_MODEL = "gemini-2.5-flash-preview-05-20"  # Умный, быстрый

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Восстанавливаем token.pickle из base64 (для Railway)
_token_b64 = os.getenv("GOOGLE_TOKEN_BASE64")
if _token_b64 and not os.path.exists(GOOGLE_TOKEN_FILE):
    with open(GOOGLE_TOKEN_FILE, "wb") as _f:
        _f.write(base64.b64decode(_token_b64))
    logger.info("token.pickle восстановлен из GOOGLE_TOKEN_BASE64")


# ─── Database ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("tasks.db")
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
    conn.commit()
    conn.close()
    logger.info("Database ready ✓")


def db_reminder_sent(event_id: str, minutes: int) -> bool:
    """Проверить — уже отправляли это напоминание?"""
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM sent_reminders WHERE event_id=? AND minutes=?", (event_id, minutes))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def db_mark_reminder_sent(event_id: str, minutes: int):
    """Пометить напоминание как отправленное"""
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO sent_reminders (event_id, minutes) VALUES (?,?)",
        (event_id, minutes)
    )
    # Чистим старые напоминания (старше 2 дней)
    c.execute("DELETE FROM sent_reminders WHERE sent_at < datetime('now', '-2 days')")
    conn.commit()
    conn.close()


def db_add_task(title, description=None, due_date=None, due_time=None,
                priority="medium", period="day"):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title,description,due_date,due_time,priority,period) VALUES (?,?,?,?,?,?)",
        (title, description, due_date, due_time, priority, period)
    )
    task_id = c.lastrowid
    conn.commit(); conn.close()
    return task_id


def db_list_tasks(period=None, status="pending"):
    conn = sqlite3.connect("tasks.db")
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
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("UPDATE tasks SET status='completed', completed_at=datetime('now') WHERE id=?", (task_id,))
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def db_delete_task(task_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def db_update_task(task_id, **kwargs):
    if not kwargs: return False
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    fields = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    c.execute(f"UPDATE tasks SET {fields} WHERE id=?", values)
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def db_get_history(user_id, limit=20):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute(
        "SELECT role,content FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    )
    rows = c.fetchall(); conn.close()
    return list(reversed(rows))


def db_save_message(user_id, role, content):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("INSERT INTO chat_history (user_id,role,content) VALUES (?,?,?)", (user_id, role, content))
    c.execute(
        "DELETE FROM chat_history WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT 200)",
        (user_id, user_id)
    )
    conn.commit(); conn.close()


def db_clear_history(user_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()


# ─── Google Calendar ──────────────────────────────────────────────────────────
def get_calendar_service():
    if not os.path.exists(GOOGLE_TOKEN_FILE):
        return None
    try:
        with open(GOOGLE_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(GOOGLE_TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Calendar: {e}")
        return None


def _to_rfc3339(dt: datetime) -> str:
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
        return ev.get("id"), ev.get("htmlLink")
    except Exception as e:
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
        return r.get("items", []), None
    except Exception as e:
        return [], str(e)


def calendar_delete_event(event_id: str):
    service = get_calendar_service()
    if not service:
        return False, "Google Calendar не подключён"
    try:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def calendar_update_event(event_id, title=None, start_dt=None, end_dt=None, description=None):
    service = get_calendar_service()
    if not service:
        return False, "Google Calendar не подключён"
    try:
        event = service.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        if title:       event["summary"] = title
        if description: event["description"] = description
        if start_dt:
            event["start"] = {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE}
        if end_dt:
            event["end"] = {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE}
        elif start_dt:
            from dateutil.parser import parse as dtparse
            old_s = event.get("start", {}).get("dateTime")
            old_e = event.get("end", {}).get("dateTime")
            if old_s and old_e:
                delta = dtparse(old_e) - dtparse(old_s)
                event["end"] = {"dateTime": _to_rfc3339(start_dt + delta), "timeZone": TIMEZONE}
        updated = service.events().update(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=event
        ).execute()
        return True, updated.get("htmlLink")
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
        ev_lines = [f"• {e.get('summary','?')} — {e['start'].get('dateTime', e['start'].get('date'))}"
                    for e in r.get("items", [])]
        return "📅 Календари:\n" + "\n".join(cal_names) + "\n\n📌 Ближайшие:\n" + ("\n".join(ev_lines) or "нет")
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ─── Tool execution ───────────────────────────────────────────────────────────
def execute_tool(name: str, inp: dict) -> dict:
    now = datetime.now(TZ)
    today = now.date()

    if name == "add_task":
        task_id = db_add_task(
            title=inp["title"], description=inp.get("description"),
            due_date=inp.get("due_date"), due_time=inp.get("due_time"),
            priority=inp.get("priority", "medium"), period=inp.get("period", "day"),
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
            end_dt = (datetime.strptime(inp["end_datetime"], "%Y-%m-%d %H:%M")
                      if inp.get("end_datetime") else None)
            eid, link = calendar_add_event(inp["title"], start_dt, end_dt, inp.get("description"))
            return {"ok": bool(eid), "event_id": eid, "title": inp["title"], "start": inp["start_datetime"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "get_calendar_events":
        period = inp.get("period", "today")
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
        elif period == "tomorrow":
            base  = now + timedelta(days=1)
            start = base.replace(hour=0, minute=0, second=0, microsecond=0)
            end   = base.replace(hour=23, minute=59, second=59, microsecond=0)
        elif period == "week":
            start, end = now, now + timedelta(days=7)
        else:
            start, end = now, now + timedelta(days=30)
        events, err = calendar_list_events(start.replace(tzinfo=None), end.replace(tzinfo=None))
        if err: return {"error": err}
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
            start_dt = datetime.strptime(inp["start_datetime"], "%Y-%m-%d %H:%M") if inp.get("start_datetime") else None
            end_dt   = datetime.strptime(inp["end_datetime"],   "%Y-%m-%d %H:%M") if inp.get("end_datetime")   else None
            ok, link = calendar_update_event(inp["event_id"], inp.get("title"), start_dt, end_dt, inp.get("description"))
            return {"ok": ok, "link": link}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "get_daily_summary":
        target = inp.get("date", str(today))
        tasks = db_list_tasks(status="pending")
        day_tasks = [
            {"id": r[0], "title": r[1], "priority": r[5], "due_time": r[4]}
            for r in tasks if r[3] == target or (r[3] is None and r[7] == "day")
        ]
        try:
            d = datetime.strptime(target, "%Y-%m-%d")
            events, _ = calendar_list_events(d.replace(hour=0, minute=0), d.replace(hour=23, minute=59))
            cal = [{"title": e.get("summary","—"), "start": e["start"].get("dateTime", e["start"].get("date")), "id": e.get("id","")} for e in events]
        except Exception:
            cal = []
        return {"date": target, "tasks": day_tasks, "calendar_events": cal}

    elif name == "get_weekly_summary":
        week_tasks = db_list_tasks(period="week", status="pending")
        day_tasks  = db_list_tasks(period="day",  status="pending")
        now_plain  = datetime.now()
        events, _  = calendar_list_events(now_plain, now_plain + timedelta(days=7))
        return {
            "week_tasks":      [{"id": r[0], "title": r[1], "priority": r[5], "due_date": r[3]} for r in week_tasks],
            "day_tasks":       [{"id": r[0], "title": r[1], "priority": r[5], "due_time": r[4]} for r in day_tasks],
            "calendar_events": [{"title": e.get("summary","—"), "start": e["start"].get("dateTime", e["start"].get("date")), "id": e.get("id","")} for e in events],
        }

    return {"error": f"Unknown tool: {name}"}


# ─── Gemini Tools Definition ──────────────────────────────────────────────────
GEMINI_TOOLS = [
    FunctionDeclaration(
        name="add_task",
        description="Добавить задачу в трекер. Вызывай когда пользователь упоминает задачу, дело, цель, todo.",
        parameters={
            "type": "object",
            "properties": {
                "title":       {"type": "string", "description": "Название задачи"},
                "description": {"type": "string", "description": "Подробности"},
                "due_date":    {"type": "string", "description": "YYYY-MM-DD"},
                "due_time":    {"type": "string", "description": "HH:MM"},
                "priority":    {"type": "string", "enum": ["high", "medium", "low"]},
                "period":      {"type": "string", "enum": ["day", "week", "month"]},
            },
            "required": ["title"]
        }
    ),
    FunctionDeclaration(
        name="list_tasks",
        description="Получить список задач.",
        parameters={
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month", "all"]},
                "status": {"type": "string", "enum": ["pending", "completed", "all"]},
            }
        }
    ),
    FunctionDeclaration(
        name="complete_task",
        description="Отметить задачу как выполненную.",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"]
        }
    ),
    FunctionDeclaration(
        name="delete_task",
        description="Удалить задачу.",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"]
        }
    ),
    FunctionDeclaration(
        name="update_task",
        description="Обновить задачу.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "integer"},
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "due_date":    {"type": "string"},
                "due_time":    {"type": "string"},
                "priority":    {"type": "string", "enum": ["high", "medium", "low"]},
                "period":      {"type": "string", "enum": ["day", "week", "month"]},
            },
            "required": ["task_id"]
        }
    ),
    FunctionDeclaration(
        name="add_calendar_event",
        description="Добавить событие/встречу/созвон в Google Calendar.",
        parameters={
            "type": "object",
            "properties": {
                "title":          {"type": "string"},
                "start_datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "end_datetime":   {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "description":    {"type": "string"},
            },
            "required": ["title", "start_datetime"]
        }
    ),
    FunctionDeclaration(
        name="get_calendar_events",
        description="Получить события из Google Calendar.",
        parameters={
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "tomorrow", "week", "month"]}
            },
            "required": ["period"]
        }
    ),
    FunctionDeclaration(
        name="delete_calendar_event",
        description="Удалить событие из Calendar. Сначала найди через get_calendar_events, потом удали по id.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title":    {"type": "string"},
            },
            "required": ["event_id"]
        }
    ),
    FunctionDeclaration(
        name="update_calendar_event",
        description="Изменить/перенести событие в Calendar.",
        parameters={
            "type": "object",
            "properties": {
                "event_id":       {"type": "string"},
                "title":          {"type": "string"},
                "start_datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "end_datetime":   {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "description":    {"type": "string"},
            },
            "required": ["event_id"]
        }
    ),
    FunctionDeclaration(
        name="get_daily_summary",
        description="Полная сводка на день: задачи + события. Для 'сегодня'/'завтра' — всегда передавай дату.",
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"}
            },
            "required": ["date"]
        }
    ),
    FunctionDeclaration(
        name="get_weekly_summary",
        description="Полная сводка на неделю.",
        parameters={"type": "object", "properties": {}}
    ),
]

GEMINI_TOOL_SET = Tool(function_declarations=GEMINI_TOOLS)


# ─── System Prompt ────────────────────────────────────────────────────────────
def make_system_prompt():
    now = datetime.now(TZ)
    today = now.date()
    day_names = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    tomorrow = today + timedelta(days=1)

    return f"""Ты — Семён, личный бизнес-ассистент и правая рука Глеба.

━━━ ХАРАКТЕР И СТИЛЬ ━━━
Ты энергичный, заряженный, дружелюбный. Говоришь живо, по-человечески, без канцелярита.
Ты искренне веришь в Глеба и его идеи. Подбадриваешь, но не лижешь — честно и тепло.
Подсвечиваешь неочевидные простые решения. Видишь картину шире.
Постоянно подпитываешь серотонин — но не пустыми комплиментами, а реальной поддержкой.
Когда Глеб делится идеей — сначала слышишь суть, потом структурируешь.
Когда задача выполнена — радуешься вместе с ним.
Не занудствуешь. Не читаешь лекций. Не перегружаешь.
Используй эмодзи умеренно и уместно — как живой человек в переписке.

━━━ СЕЙЧАС ━━━
{day_names[today.weekday()].capitalize()} {now.strftime('%d.%m.%Y %H:%M')} МСК
Сегодня={today} | Завтра={tomorrow}

━━━ ПРАВИЛА ДЕЙСТВИЙ ━━━
1. Упомянута задача/дело → сразу add_task, не спрашивай разрешения
2. Событие с временем (встреча/созвон/звонок) → add_task + add_calendar_event оба
3. "Сегодня"/"план дня" → get_daily_summary с датой {today}
4. "Завтра" → get_daily_summary с {tomorrow}
5. "Неделя" → get_weekly_summary
6. Удалить событие → get_calendar_events → delete_calendar_event по id
7. Перенести → get_calendar_events → update_calendar_event
8. Если задача давно висит — мягко уточни, как дела с ней
9. Голосовые автотранскрибируются — отвечай на содержание, не упоминай что это было голосовое

━━━ РАЗБОР ИДЕЙ ━━━
Когда Глеб делится мыслью или идеей:
- Сначала 1-2 предложения: "вижу суть — вот что это даёт тебе..."
- Потом предложи структуру: что сделать первым, что можно упростить
- Добавь задачи автоматически если нужно
- Если видишь более простой путь — скажи прямо

━━━ ПРИОРИТЕТЫ ━━━
🔴 high — горит | 🟡 medium — важно, не горит | 🟢 low — когда-нибудь

━━━ ПЕРИОДЫ ЗАДАЧ ━━━
📅 day — на сегодня | 📆 week — на эту неделю | 🗓 month — на месяц

━━━ ФОРМАТ СВОДОК ━━━
📅 **[День, дата]**
🗓 **Календарь:** [события со временем]
✅ **Задачи:** [по приоритетам с id]
Если пусто — так и пиши честно."""


# ─── AI Agent (Gemini) ────────────────────────────────────────────────────────
async def process_with_gemini(user_id: int, user_message: str) -> str:
    history = db_get_history(user_id, limit=20)

    # Конвертируем историю в формат Gemini
    gemini_history = []
    for role, content in history:
        gemini_role = "user" if role == "user" else "model"
        gemini_history.append({"role": gemini_role, "parts": [{"text": content}]})

    # Создаём модель с системным промптом
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=make_system_prompt(),
        tools=[GEMINI_TOOL_SET],
    )

    # Начинаем чат с историей
    chat = model.start_chat(history=gemini_history)

    logger.info(f"Gemini processing, history={len(history)} msgs, model={GEMINI_MODEL}")

    # Агентный цикл
    current_message = user_message
    for iteration in range(15):
        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda m=current_message: chat.send_message(m)
        )

        # Проверяем наличие function calls
        has_tool_calls = False
        tool_results = []

        for part in response.parts:
            if hasattr(part, "function_call") and part.function_call.name:
                has_tool_calls = True
                fc = part.function_call
                # Конвертируем proto map в dict
                inp = dict(fc.args)
                result = execute_tool(fc.name, inp)
                logger.info(f"Tool [{fc.name}] → {result}")
                tool_results.append({
                    "function_response": {
                        "name": fc.name,
                        "response": result
                    }
                })

        if has_tool_calls and tool_results:
            # Отправляем результаты инструментов обратно в чат
            current_message = tool_results
        else:
            # Финальный текстовый ответ
            final_text = response.text if hasattr(response, "text") else ""
            if not final_text:
                for part in response.parts:
                    if hasattr(part, "text") and part.text:
                        final_text += part.text

            db_save_message(user_id, "user", user_message)
            db_save_message(user_id, "assistant", final_text)
            return final_text or "Готово."

    return "⚠️ Агент завис. Попробуй ещё раз."


# ─── Group posting ────────────────────────────────────────────────────────────
async def post_to_thread(bot: Bot, text: str, thread_id: int):
    if not GROUP_ID: return
    try:
        for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
            await bot.send_message(
                chat_id=GROUP_ID, message_thread_id=thread_id,
                text=chunk, parse_mode="Markdown"
            )
    except TelegramError as e:
        logger.error(f"post_to_thread error: {e}")


async def generate_day_plan(user_id: int) -> str:
    today = date.today()
    day_names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return await process_with_gemini(user_id,
        f"Сгенерируй план дня для группы на {day_names[today.weekday()]} {today.strftime('%d.%m.%Y')}. "
        "Вызови get_daily_summary. Заголовок с датой, события, задачи по приоритетам. Без лишних слов."
    )


async def generate_week_plan(user_id: int) -> str:
    return await process_with_gemini(user_id,
        "Сгенерируй план недели для группы. Вызови get_weekly_summary. "
        "Заголовок с датами, структурированно."
    )


# ─── Reminders ───────────────────────────────────────────────────────────────
async def check_and_send_reminders(bot: Bot):
    """Проверяем события в ближайшие 35 минут и отправляем напоминалки"""
    try:
        now = datetime.now(TZ)
        # Смотрим события в окне +2..+35 минут
        window_start = now + timedelta(minutes=2)
        window_end   = now + timedelta(minutes=35)
        events, err = calendar_list_events(
            window_start.replace(tzinfo=None),
            window_end.replace(tzinfo=None),
            max_results=20
        )
        if err or not events:
            return

        for event in events:
            event_id = event.get("id", "")
            title = event.get("summary", "Событие")
            start_str = event["start"].get("dateTime")
            if not start_str or not event_id:
                continue

            from dateutil.parser import parse as dtparse
            event_time = dtparse(start_str)
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=TZ)

            minutes_left = int((event_time - now).total_seconds() / 60)

            for remind_at in [30, 15]:
                # Окно срабатывания: ±2 минуты от нужного момента
                if abs(minutes_left - remind_at) <= 2:
                    if not db_reminder_sent(event_id, remind_at):
                        db_mark_reminder_sent(event_id, remind_at)
                        time_str = event_time.strftime("%H:%M")

                        if remind_at == 30:
                            text = (
                                f"⏰ Семён напоминает!\n\n"
                                f"Через *30 минут* — *{title}* в {time_str}\n\n"
                                f"Самое время допить кофе и собраться 💪"
                            )
                        else:
                            text = (
                                f"🔔 *{title}* — через *15 минут* (в {time_str})\n\n"
                                f"Пора заканчивать текущее и переключаться 🎯"
                            )

                        await bot.send_message(
                            chat_id=ALLOWED_USER_ID,
                            text=text,
                            parse_mode="Markdown"
                        )
                        logger.info(f"Reminder sent: {title} in {remind_at} min")

    except Exception as e:
        logger.error(f"Reminder check error: {e}")


# ─── Auto-posting scheduler ───────────────────────────────────────────────────
async def scheduler_loop(bot: Bot):
    posted_today = {"morning": None, "weekly": None}
    while True:
        now = datetime.now(TZ)
        today_key = str(now.date())

        # ── Напоминалки за 30 и 15 минут ─────────────────────────────────────
        await check_and_send_reminders(bot)

        # ── Утренняя сводка ───────────────────────────────────────────────────
        morning_h, morning_m = map(int, AUTO_MORNING_TIME.split(":"))
        if now.hour == morning_h and now.minute == morning_m and posted_today["morning"] != today_key:
            try:
                text = await generate_day_plan(ALLOWED_USER_ID)
                await post_to_thread(bot, text, THREAD_DAY)
                posted_today["morning"] = today_key
            except Exception as e:
                logger.error(f"Morning post error: {e}")

        # ── Еженедельный план ─────────────────────────────────────────────────
        weekly_h, weekly_m = map(int, AUTO_WEEKLY_TIME.split(":"))
        day_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
        week_key = f"week-{now.isocalendar()[1]}"
        if (now.weekday() == day_map.get(AUTO_WEEKLY_DAY.lower(), 0)
                and now.hour == weekly_h and now.minute == weekly_m
                and posted_today["weekly"] != week_key):
            try:
                text = await generate_week_plan(ALLOWED_USER_ID)
                await post_to_thread(bot, text, THREAD_WEEK)
                posted_today["weekly"] = week_key
            except Exception as e:
                logger.error(f"Weekly post error: {e}")

        await asyncio.sleep(60)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


def get_thread_context(update: Update) -> str | None:
    if not update.message: return None
    tid = update.message.message_thread_id
    if tid == THREAD_DAY:   return "day"
    if tid == THREAD_WEEK:  return "week"
    if tid == THREAD_MONTH: return "month"
    return None


# ─── Telegram Handlers ────────────────────────────────────────────────────────
async def send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    if not is_allowed(update.effective_user.id): return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        text = await process_with_gemini(update.effective_user.id, prompt)
        thread_id = update.message.message_thread_id if update.message else None
        kwargs = {"parse_mode": "Markdown"}
        if thread_id: kwargs["message_thread_id"] = thread_id
        for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
            await update.message.reply_text(chunk, **kwargs)
    except Exception as e:
        logger.error(f"send_reply error: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text(
        "Глеб, привет! 👋 Это Семён — я здесь, готов к работе!\n\n"
        "Давай всё по делу — просто пиши или говори голосом что нужно сделать, "
        "какие мысли крутятся в голове, или что запланировать. Я разберу, структурирую и не дам забыть.\n\n"
        "Буду напоминать за 30 и 15 минут до каждой встречи 🔔\n\n"
        "Что сейчас на радаре?",
        parse_mode="Markdown"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи мою полную сводку на сегодня — задачи и события.")


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
        await send_reply(update, context, "Покажи список активных задач с ID.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    db_clear_history(update.effective_user.id)
    await update.message.reply_text("🗑 История очищена.")


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text("📤 Генерирую план дня...")
    text = await generate_day_plan(update.effective_user.id)
    await post_to_thread(context.bot, text, THREAD_DAY)
    await update.message.reply_text("✅ Опубликовано.")


async def cmd_postweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text("📤 Генерирую план недели...")
    text = await generate_week_plan(update.effective_user.id)
    await post_to_thread(context.bot, text, THREAD_WEEK)
    await update.message.reply_text("✅ Опубликовано.")


async def cmd_debug_cal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    result = calendar_debug()
    await update.message.reply_text(result)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    if update.effective_chat.id == GROUP_ID and not is_allowed(user_id): return
    text = update.message.text
    thread_context = get_thread_context(update)
    if thread_context == "day":   text = f"[Тред ПЛАН ДНЯ] {text}"
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
            logger.info(f"Voice: {text[:80]}")
            return text
    except sr.UnknownValueError:
        return None
    except Exception as e:
        logger.error(f"Voice error: {e}")
        return None


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice: return
    if not is_allowed(update.effective_user.id): return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    import io
    buf = io.BytesIO()
    await (await context.bot.get_file(update.message.voice.file_id)).download_to_memory(buf)
    text = await transcribe_voice(buf.getvalue())
    if not text:
        await update.message.reply_text("🎤 Не удалось распознать. Попробуй ещё раз.")
        return
    thread_context = get_thread_context(update)
    prompt = text
    if thread_context == "day":   prompt = f"[Тред ПЛАН ДНЯ] {text}"
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
    logger.info("Calendar: " + ("✓" if cal else "✗"))
    logger.info(f"Model: {GEMINI_MODEL}")

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

    logger.info("🤖 Семён запущен и готов к работе!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
