#!/usr/bin/env python3
"""
AI Business Assistant — Глеб
Telegram (личка + группа с тредами) + Claude AI + Google Calendar + Task Tracker
"""

import os
import json
import sqlite3
import logging
import pickle
import asyncio
import base64
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import TelegramError

import anthropic
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))

# Группа alta трекинг
GROUP_ID           = int(os.getenv("GROUP_ID", "0"))
THREAD_DAY         = int(os.getenv("THREAD_DAY", "9"))    # План дня Глеба
THREAD_WEEK        = int(os.getenv("THREAD_WEEK", "7"))   # План недели
THREAD_MONTH       = int(os.getenv("THREAD_MONTH", "6"))  # План месяца

GOOGLE_CREDS_FILE  = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE  = os.getenv("GOOGLE_TOKEN_FILE", "token.pickle")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
TIMEZONE           = os.getenv("TIMEZONE", "Europe/Moscow")
TZ                 = ZoneInfo(TIMEZONE)

AUTO_POST_ENABLED  = os.getenv("AUTO_POST_ENABLED", "true").lower() == "true"
AUTO_MORNING_TIME  = os.getenv("AUTO_MORNING_TIME", "09:00")
AUTO_WEEKLY_DAY    = os.getenv("AUTO_WEEKLY_DAY", "monday")
AUTO_WEEKLY_TIME   = os.getenv("AUTO_WEEKLY_TIME", "08:30")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Восстанавливаем token.pickle из base64 если задана переменная (для Railway)
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
    conn.commit()
    conn.close()
    logger.info("Database ready ✓")


def db_add_task(title, description=None, due_date=None, due_time=None,
                priority="medium", period="day"):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title,description,due_date,due_time,priority,period) "
        "VALUES (?,?,?,?,?,?)",
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


def db_get_history(user_id, limit=10):
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
        "(SELECT id FROM chat_history WHERE user_id=? ORDER BY created_at DESC LIMIT 100)",
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
    """Конвертируем naive datetime (MSK) → RFC3339 с timezone offset для Google API"""
    aware = dt.replace(tzinfo=TZ)
    return aware.isoformat()


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
        logger.info(f"Calendar event created: {ev.get('id')} — {title}")
        return ev.get("id"), ev.get("htmlLink")
    except Exception as e:
        logger.error(f"calendar_add_event error: {e}")
        return None, str(e)


def calendar_list_events(start_dt, end_dt, max_results=30):
    service = get_calendar_service()
    if not service:
        logger.error("calendar_list_events: no service")
        return [], "Google Calendar не подключён"
    try:
        time_min = _to_rfc3339(start_dt)
        time_max = _to_rfc3339(end_dt)
        logger.info(f"Calendar query: {time_min} → {time_max}")
        r = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        items = r.get("items", [])
        logger.info(f"Calendar returned {len(items)} events")
        return items, None
    except Exception as e:
        logger.error(f"calendar_list_events error: {e}")
        return [], str(e)


def calendar_delete_event(event_id: str):
    service = get_calendar_service()
    if not service:
        return False, "Google Calendar не подключён"
    try:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        logger.info(f"Calendar event deleted: {event_id}")
        return True, None
    except Exception as e:
        logger.error(f"calendar_delete_event error: {e}")
        return False, str(e)


def calendar_update_event(event_id: str, title=None, start_dt=None, end_dt=None, description=None):
    service = get_calendar_service()
    if not service:
        return False, "Google Calendar не подключён"
    try:
        event = service.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        if title:
            event["summary"] = title
        if description is not None:
            event["description"] = description
        if start_dt:
            event["start"] = {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE}
        if end_dt:
            event["end"] = {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE}
        elif start_dt:
            # Сдвигаем конец на то же смещение
            old_start = event.get("start", {}).get("dateTime")
            old_end   = event.get("end",   {}).get("dateTime")
            if old_start and old_end:
                from dateutil.parser import parse as dtparse
                delta = dtparse(old_end) - dtparse(old_start)
                new_end = start_dt + delta
                event["end"] = {"dateTime": _to_rfc3339(new_end), "timeZone": TIMEZONE}
        updated = service.events().update(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=event
        ).execute()
        logger.info(f"Calendar event updated: {event_id}")
        return True, updated.get("htmlLink")
    except Exception as e:
        logger.error(f"calendar_update_event error: {e}")
        return False, str(e)


def calendar_debug() -> str:
    """Диагностика: показывает список всех календарей и ближайшие 5 событий"""
    service = get_calendar_service()
    if not service:
        return "❌ Нет подключения к Google Calendar"
    try:
        # Список календарей
        cals = service.calendarList().list().execute()
        cal_names = [f"• {c.get('summary')} (id: {c.get('id')})" for c in cals.get("items", [])]

        # Ближайшие 5 событий
        now = datetime.now(TZ)
        r = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=now.isoformat(),
            maxResults=5,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = r.get("items", [])
        ev_lines = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            ev_lines.append(f"• {e.get('summary','?')} — {start}")

        result = "📅 Календари:\n" + "\n".join(cal_names)
        result += "\n\n📌 Ближайшие события:\n"
        result += "\n".join(ev_lines) if ev_lines else "нет событий"
        return result
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ─── Claude Tools ─────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "add_task",
        "description": "Добавить задачу в трекер. Используй когда пользователь говорит о задаче, деле, цели, todo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "due_date":    {"type": "string", "description": "YYYY-MM-DD"},
                "due_time":    {"type": "string", "description": "HH:MM"},
                "priority":    {"type": "string", "enum": ["high", "medium", "low"]},
                "period":      {"type": "string", "enum": ["day", "week", "month"]},
            },
            "required": ["title"]
        }
    },
    {
        "name": "list_tasks",
        "description": "Получить список задач по периоду и статусу.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month", "all"]},
                "status": {"type": "string", "enum": ["pending", "completed", "all"]},
            }
        }
    },
    {
        "name": "complete_task",
        "description": "Отметить задачу как выполненную по ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"]
        }
    },
    {
        "name": "delete_task",
        "description": "Удалить задачу по ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"]
        }
    },
    {
        "name": "update_task",
        "description": "Обновить поля задачи.",
        "input_schema": {
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
    },
    {
        "name": "add_calendar_event",
        "description": "Добавить событие / встречу / созвон в Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":          {"type": "string"},
                "start_datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "end_datetime":   {"type": "string", "description": "YYYY-MM-DD HH:MM (необязательно)"},
                "description":    {"type": "string"},
            },
            "required": ["title", "start_datetime"]
        }
    },
    {
        "name": "get_calendar_events",
        "description": "Получить события из Google Calendar на период.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "tomorrow", "week", "month"]}
            },
            "required": ["period"]
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Удалить событие из Google Calendar по его ID. Сначала получи список событий через get_calendar_events, найди нужное по названию, затем удали по id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID события из поля 'id' в списке событий"},
                "title":    {"type": "string", "description": "Название события (для подтверждения)"},
            },
            "required": ["event_id"]
        }
    },
    {
        "name": "update_calendar_event",
        "description": "Изменить существующее событие в Google Calendar — перенести время, переименовать и т.д.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id":       {"type": "string", "description": "ID события"},
                "title":          {"type": "string", "description": "Новое название (необязательно)"},
                "start_datetime": {"type": "string", "description": "Новое время начала YYYY-MM-DD HH:MM (необязательно)"},
                "end_datetime":   {"type": "string", "description": "Новое время окончания YYYY-MM-DD HH:MM (необязательно)"},
                "description":    {"type": "string", "description": "Новое описание (необязательно)"},
            },
            "required": ["event_id"]
        }
    },
    {
        "name": "get_daily_summary",
        "description": "Сводка на день: задачи + события из календаря. Используй для 'что сегодня', 'что завтра', 'план на [дату]'. Всегда передавай конкретную дату.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD. Для 'завтра' — передай завтрашнюю дату. Для 'сегодня' — сегодняшнюю."}
            }
        }
    },
    {
        "name": "get_weekly_summary",
        "description": "Сводка на неделю: все задачи + события. Используй для 'план недели', 'что на неделю'.",
        "input_schema": {"type": "object", "properties": {}}
    }
]


def execute_tool(name: str, inp: dict) -> dict:
    now = datetime.now(TZ)
    today = now.date()

    if name == "add_task":
        task_id = db_add_task(
            title=inp["title"], description=inp.get("description"),
            due_date=inp.get("due_date"), due_time=inp.get("due_time"),
            priority=inp.get("priority", "medium"), period=inp.get("period", "day"),
        )
        return {"ok": True, "task_id": task_id}

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
        return {"ok": db_complete_task(inp["task_id"])}

    elif name == "delete_task":
        return {"ok": db_delete_task(inp["task_id"])}

    elif name == "update_task":
        tid = inp.pop("task_id")
        return {"ok": db_update_task(tid, **inp) if inp else False}

    elif name == "add_calendar_event":
        try:
            start_dt = datetime.strptime(inp["start_datetime"], "%Y-%m-%d %H:%M")
            end_dt = (datetime.strptime(inp["end_datetime"], "%Y-%m-%d %H:%M")
                      if inp.get("end_datetime") else None)
            eid, link = calendar_add_event(inp["title"], start_dt, end_dt, inp.get("description"))
            return {"ok": bool(eid), "event_id": eid, "link": link}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "get_calendar_events":
        period = inp.get("period", "week")
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
        events, err = calendar_list_events(
            start.replace(tzinfo=None), end.replace(tzinfo=None)
        )
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
                event_id=inp["event_id"],
                title=inp.get("title"),
                start_dt=start_dt,
                end_dt=end_dt,
                description=inp.get("description"),
            )
            return {"ok": ok, "link": link}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "get_daily_summary":
        target = inp.get("date", str(today))
        tasks = db_list_tasks(status="pending")
        day_tasks = [
            {"id": r[0], "title": r[1], "priority": r[5]}
            for r in tasks
            if r[3] == target or (r[3] is None and r[7] == "day")
        ]
        try:
            d = datetime.strptime(target, "%Y-%m-%d")
            events, _ = calendar_list_events(d.replace(hour=0, minute=0), d.replace(hour=23, minute=59))
            cal = [{"title": e.get("summary", "—"),
                    "start": e["start"].get("dateTime", e["start"].get("date"))} for e in events]
        except Exception:
            cal = []
        return {"date": target, "tasks": day_tasks, "calendar_events": cal}

    elif name == "get_weekly_summary":
        week_tasks = db_list_tasks(period="week", status="pending")
        day_tasks  = db_list_tasks(period="day",  status="pending")
        now_plain  = datetime.now()
        events, _  = calendar_list_events(now_plain, now_plain + timedelta(days=7))
        return {
            "week_tasks": [{"id": r[0], "title": r[1], "priority": r[5], "due_date": r[3]} for r in week_tasks],
            "day_tasks":  [{"id": r[0], "title": r[1], "priority": r[5]} for r in day_tasks],
            "calendar_events": [
                {"title": e.get("summary", "—"),
                 "start": e["start"].get("dateTime", e["start"].get("date"))} for e in events
            ],
        }

    return {"error": f"Unknown tool: {name}"}


# ─── System Prompt ────────────────────────────────────────────────────────────
def make_system_prompt():
    today = date.today()
    day_names = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    tomorrow = today + timedelta(days=1)
    return f"""Ты — бизнес-ассистент Глеба (Макс). Русский, кратко, по делу.
Сегодня: {day_names[today.weekday()]} {today.strftime('%d.%m.%Y')} | Завтра: {tomorrow.strftime('%d.%m.%Y')} (МСК)

ПРАВИЛА (строго):
- Задача упомянута → add_task немедленно
- Задача с конкретным временем → add_task + add_calendar_event (оба вызова)
- "сегодня"/"завтра"/"план" → get_daily_summary с датой {today} или {tomorrow}
- "неделя" → get_weekly_summary
- После добавления — 1-2 строки подтверждения

Приоритеты: 🔴high 🟡medium 🟢low | Периоды: 📅день 📆неделя 🗓месяц"""


# ─── AI Agent ─────────────────────────────────────────────────────────────────
async def process_with_claude(user_id: int, user_message: str) -> str:
    history = db_get_history(user_id)
    messages = [{"role": r, "content": c} for r, c in history]
    messages.append({"role": "user", "content": user_message})

    for _ in range(10):
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=make_system_prompt(),
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, dict(block.input))
                    logger.info(f"Tool [{block.name}] → {result}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            final_text = "".join(b.text for b in response.content if hasattr(b, "text"))
            db_save_message(user_id, "user", user_message)
            db_save_message(user_id, "assistant", final_text)
            return final_text

    return "⚠️ Агент завис. Попробуй ещё раз."


# ─── Group posting helpers ────────────────────────────────────────────────────
async def post_to_thread(bot: Bot, text: str, thread_id: int):
    """Отправить сообщение в конкретный тред группы"""
    if not GROUP_ID:
        logger.warning("GROUP_ID не задан")
        return
    try:
        for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
            await bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                text=chunk,
                parse_mode="Markdown"
            )
        logger.info(f"Posted to thread {thread_id} ✓")
    except TelegramError as e:
        logger.error(f"post_to_thread error: {e}")


async def generate_day_plan(user_id: int) -> str:
    return await process_with_claude(user_id,
        "Сгенерируй структурированный план дня для публикации в группу. "
        "Формат: задачи по приоритетам + события из календаря. "
        "Заголовок с датой. Без лишних слов, только конкретика."
    )


async def generate_week_plan(user_id: int) -> str:
    return await process_with_claude(user_id,
        "Сгенерируй план на неделю для публикации в группу. "
        "Задачи на неделю + ближайшие события в календаре. "
        "Заголовок с датами недели. Чётко и по делу."
    )


# ─── Auto-posting scheduler ───────────────────────────────────────────────────
async def scheduler_loop(bot: Bot):
    """Фоновая задача — автопостинг планов в группу"""
    logger.info(f"Scheduler started. Morning={AUTO_MORNING_TIME}, Weekly={AUTO_WEEKLY_DAY} {AUTO_WEEKLY_TIME}")
    posted_today = {"morning": None, "weekly": None}

    while True:
        now = datetime.now(TZ)
        today_key = str(now.date())

        # ── Утренняя сводка дня ──────────────────────────────────────────────
        morning_h, morning_m = map(int, AUTO_MORNING_TIME.split(":"))
        if (now.hour == morning_h and now.minute == morning_m
                and posted_today["morning"] != today_key):
            try:
                logger.info("Auto-posting daily plan...")
                text = await generate_day_plan(ALLOWED_USER_ID)
                await post_to_thread(bot, text, THREAD_DAY)
                posted_today["morning"] = today_key
            except Exception as e:
                logger.error(f"Morning post error: {e}")

        # ── Еженедельный план (понедельник) ───────────────────────────────────
        weekly_h, weekly_m = map(int, AUTO_WEEKLY_TIME.split(":"))
        day_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
                   "friday":4,"saturday":5,"sunday":6}
        target_weekday = day_map.get(AUTO_WEEKLY_DAY.lower(), 0)
        week_key = f"week-{now.isocalendar()[1]}"
        if (now.weekday() == target_weekday
                and now.hour == weekly_h and now.minute == weekly_m
                and posted_today["weekly"] != week_key):
            try:
                logger.info("Auto-posting weekly plan...")
                text = await generate_week_plan(ALLOWED_USER_ID)
                await post_to_thread(bot, text, THREAD_WEEK)
                posted_today["weekly"] = week_key
            except Exception as e:
                logger.error(f"Weekly post error: {e}")

        await asyncio.sleep(60)  # проверяем каждую минуту


# ─── Access check ─────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


def get_thread_context(update: Update) -> str | None:
    """Определить контекст треда группы"""
    if not update.message:
        return None
    tid = update.message.message_thread_id
    if tid == THREAD_DAY:
        return "day"
    elif tid == THREAD_WEEK:
        return "week"
    elif tid == THREAD_MONTH:
        return "month"
    return None


# ─── Telegram Handlers ────────────────────────────────────────────────────────
async def send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    if not is_allowed(update.effective_user.id):
        return  # В группе тихо игнорируем чужих
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        text = await process_with_claude(update.effective_user.id, prompt)
        thread_id = update.message.message_thread_id if update.message else None
        kwargs = {"parse_mode": "Markdown"}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
            await update.message.reply_text(chunk, **kwargs)
    except Exception as e:
        logger.error(f"send_reply error: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context,
        "Пользователь запустил бота командой /start. "
        "Поприветствуй его как Макс, скажи что умеешь, и спроси что на сегодня."
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи мою сводку на сегодня: задачи + события в календаре.")


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
        await send_reply(update, context, "Покажи список активных задач — выберу что выполнено.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    db_clear_history(update.effective_user.id)
    await update.message.reply_text("🗑 История очищена.")


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вручную опубликовать план дня в THREAD_DAY группы"""
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("📤 Публикую план дня в группу...")
    text = await generate_day_plan(update.effective_user.id)
    await post_to_thread(context.bot, text, THREAD_DAY)
    await update.message.reply_text("✅ Опубликовано в тред «План дня Глеба»")


async def cmd_debug_cal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика Google Calendar — показывает все календари и ближайшие события"""
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("🔍 Проверяю Google Calendar...")
    result = calendar_debug()
    await update.message.reply_text(result)


async def cmd_postweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вручную опубликовать план недели в THREAD_WEEK"""
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("📤 Публикую план недели в группу...")
    text = await generate_week_plan(update.effective_user.id)
    await post_to_thread(context.bot, text, THREAD_WEEK)
    await update.message.reply_text("✅ Опубликовано в тред «План недели»")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    # В группе отвечаем только Глебу или если упомянули бота
    is_group = update.effective_chat.id == GROUP_ID
    if is_group and not is_allowed(user_id):
        return  # Не наш пользователь — игнорируем

    text = update.message.text
    thread_context = get_thread_context(update)

    # Добавляем контекст треда в промпт для правильного поведения
    if thread_context == "day":
        text = f"[Сообщение из треда ПЛАН ДНЯ] {text}"
    elif thread_context == "week":
        text = f"[Сообщение из треда ПЛАН НЕДЕЛИ] {text}"
    elif thread_context == "month":
        text = f"[Сообщение из треда ПЛАН МЕСЯЦА] {text}"

    await send_reply(update, context, text)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    """Запускаем планировщик после инициализации приложения"""
    if AUTO_POST_ENABLED and GROUP_ID:
        asyncio.create_task(scheduler_loop(application.bot))
        logger.info("Scheduler task created ✓")


def main():
    init_db()

    cal = get_calendar_service()
    logger.info("Google Calendar: " + ("✓ подключён" if cal else "✗ не подключён"))
    logger.info(f"Группа: {GROUP_ID} | Треды: день={THREAD_DAY} неделя={THREAD_WEEK} месяц={THREAD_MONTH}")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("today",    cmd_today))
    app.add_handler(CommandHandler("week",     cmd_week))
    app.add_handler(CommandHandler("month",    cmd_month))
    app.add_handler(CommandHandler("tasks",    cmd_tasks))
    app.add_handler(CommandHandler("done",     cmd_done))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(CommandHandler("debug_cal", cmd_debug_cal))
    app.add_handler(CommandHandler("post",     cmd_post))
    app.add_handler(CommandHandler("postweek", cmd_postweek))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
