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
import time
import urllib.request
import urllib.error
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
# OAuth client config — нужно чтобы обновлять access-токен, если в token.pickle этих полей нет
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URI     = os.getenv("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")
TIMEZONE           = os.getenv("TIMEZONE", "Europe/Moscow")
TZ                 = ZoneInfo(TIMEZONE)

AUTO_POST_ENABLED  = os.getenv("AUTO_POST_ENABLED", "true").lower() == "true"
AUTO_MORNING_TIME  = os.getenv("AUTO_MORNING_TIME", "09:00")
AUTO_WEEKLY_DAY    = os.getenv("AUTO_WEEKLY_DAY", "monday")
AUTO_WEEKLY_TIME   = os.getenv("AUTO_WEEKLY_TIME", "08:30")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
VERSION = "6.2"

AUTO_CHECKIN_ENABLED = os.getenv("AUTO_CHECKIN_ENABLED", "true").lower() == "true"
AUTO_CHECKIN_TIME    = os.getenv("AUTO_CHECKIN_TIME", "18:00")

# Вечерняя стратегическая сводка (итог дня + идеи на завтра взглядом предпринимателя)
AUTO_EVENING_ENABLED = os.getenv("AUTO_EVENING_ENABLED", "true").lower() == "true"
AUTO_EVENING_TIME    = os.getenv("AUTO_EVENING_TIME", "21:00")
# Недельный обзор идей (воскресенье)
AUTO_IDEAREVIEW_TIME = os.getenv("AUTO_IDEAREVIEW_TIME", "19:00")
# Большая амбициозная цель — ориентир для стратегического мышления
BIG_GOAL = os.getenv("BIG_GOAL", "10 млн ₽ чистой прибыли")

# Путь к БД — берём из переменной, чтобы Railway Volume можно было подключить
DB_PATH = os.getenv("DB_PATH", "tasks.db")

# Финансовый контур — KPI из task-board (раздел «Экономика»)
# FINANCE_API_URL   = https://<task-board>.up.railway.app/api/kpi
# FINANCE_API_TOKEN = совпадает с SEMYON_TOKEN в task-board
FINANCE_API_URL   = os.getenv("FINANCE_API_URL", "")
FINANCE_API_TOKEN = os.getenv("FINANCE_API_TOKEN", "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
# Убираем мусорный warning от googleapiclient (file_cache)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Восстанавливаем token.pickle из base64 (для Railway)
_token_b64 = os.getenv("GOOGLE_TOKEN_BASE64")
if _token_b64 and not os.path.exists(GOOGLE_TOKEN_FILE):
    with open(GOOGLE_TOKEN_FILE, "wb") as _f:
        _f.write(base64.b64decode(_token_b64))
    logger.info("token.pickle восстановлен из GOOGLE_TOKEN_BASE64")


# ─── Фильтр AI-напоминаний ────────────────────────────────────────────────────
def filter_ai_reminders(text: str) -> str:
    """
    Последний рубеж защиты: убираем из ответа ИИ фразы вида
    'Глеб, через 15 минут встреча!' и блоки с таймингом события.
    Планировщик сам отправляет напоминания — ИИ не должен этого делать.
    """
    # "Глеб, через X минут встреча/событие/созвон..." (целая строка)
    text = re.sub(
        r'[А-ЯЁа-яёA-Za-z]+,?\s+через\s+\d+\s+минут[а-яё]*\s+[а-яёА-ЯЁ\w]+[^\n]*\n?',
        '', text, flags=re.IGNORECASE
    )
    # строки вида "⏰ 07:00–09:30 (150 мин)" — блок с диапазоном времени события
    text = re.sub(
        r'[⏰🕐🔔]?\s*\d{1,2}:\d{2}[–—-]\d{1,2}:\d{2}\s*\(\d+\s*мин\)[^\n]*\n?',
        '', text, flags=re.IGNORECASE
    )
    # "через X минут встреча/созвон/слот" в середине предложения
    text = re.sub(
        r'через\s+\d+\s+минут[а-яё]*\s+(встреч|созвон|слот|событи)\w*',
        '', text, flags=re.IGNORECASE
    )
    return text.strip()


# ─── Чистка ответа от технического мусора ──────────────────────────────────────
_TOOL_NAMES_RE = (
    "add_idea|update_idea|list_ideas|set_goal|complete_goal|list_goals|"
    "add_project|update_project|list_projects|add_task|add_google_task|update_task|"
    "complete_task|delete_task|list_tasks|list_google_tasks|complete_google_task|"
    "delete_google_task|add_calendar_event|update_calendar_event|delete_calendar_event|"
    "get_calendar_events|get_daily_summary|get_weekly_summary|get_business_kpi|"
    "add_waiting|list_waiting|resolve_waiting"
)

def clean_ai_output(text: str) -> str:
    """
    Убираем из ответа технический мусор: напечатанные вызовы инструментов,
    служебные пометки в скобках, markdown-заголовки и кривые маркеры —
    чтобы сообщение выглядело по-человечески, а не как лог.
    """
    if not text:
        return text

    # 1. Целые строки-вызовы инструментов: "* add_idea(...)" / "add_idea(...)"
    text = re.sub(rf'(?im)^\s*[\*\-•]?\s*(?:{_TOOL_NAMES_RE})\s*\([^\n]*\)\s*\*?\s*$', '', text)
    # 2. Инлайн-вызовы инструментов внутри текста
    text = re.sub(rf'(?:{_TOOL_NAMES_RE})\s*\([^\n)]*\)', '', text)
    # 3. Служебные пометки в скобках: "(Идею зафиксировал в банке)", "(идея сохранена)"
    text = re.sub(r'\(\s*идею?\b[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\(\s*(?:зафиксировал|сохранил|сохранено|добавил[оа]?|записал|внёс)[^)]*\)',
                  '', text, flags=re.IGNORECASE)
    # 4. Преамбулы вида "Идеи сохранил в банк:" — целой строкой
    text = re.sub(r'(?im)^\s*идеи?\s+(?:сохранил|сохранен[ыо]|зафиксирован[ыо]?|добавил|записал)[^\n]*:?\s*$',
                  '', text)
    # 5. Markdown-заголовки (#, ##, ###) — Telegram их не рендерит
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s*', '', text)
    # 6. Горизонтальные линии (---, ***, ___)
    text = re.sub(r'(?m)^\s*([-*_]\s*){3,}\s*$', '', text)
    # 7. Маркеры списков "* " / "- " в начале строки → "• "
    text = re.sub(r'(?m)^(\s*)[\*\-]\s+', r'\1• ', text)
    # 8. Висячая звёздочка после двоеточия "Суть:*" → "Суть:"
    text = re.sub(r':\s*\*+', ':', text)
    # 9. Чиним хвостовые пробелы и схлопываем пустые строки
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
    # ── v6.0 ── Бизнес-операционка: цели, проекты, идеи, ожидания ──────────────
    # Цели и фокус. scope: month | week | day_focus | bottleneck
    # period_key: '2026-06' (месяц) | '2026-W23' (неделя) | '2026-06-02' (день/узкое место)
    c.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            text        TEXT NOT NULL,
            period_key  TEXT,
            status      TEXT DEFAULT 'active',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    # Проекты с рейтингом: рычаг = прибыль × вероятность × стратегичность / время
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            status           TEXT DEFAULT 'active',
            expected_profit  INTEGER,
            success_prob     INTEGER,
            time_required    INTEGER,
            strategic_value  INTEGER,
            comment          TEXT,
            created_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    # Второй мозг для идей. category: content | product | partnership | automation | other
    c.execute("""
        CREATE TABLE IF NOT EXISTS ideas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT NOT NULL,
            category    TEXT DEFAULT 'other',
            status      TEXT DEFAULT 'new',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    # Ожидания от других людей
    c.execute("""
        CREATE TABLE IF NOT EXISTS waiting_for (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            what        TEXT NOT NULL,
            who         TEXT,
            due_date    TEXT,
            status      TEXT DEFAULT 'waiting',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # ── Миграции колонок tasks (идемпотентно) ──
    c.execute("PRAGMA table_info(tasks)")
    existing_cols = {row[1] for row in c.fetchall()}
    if "energy_type" not in existing_cols:
        # deep | comm | ops | creative | routine
        c.execute("ALTER TABLE tasks ADD COLUMN energy_type TEXT")
    if "project_id" not in existing_cols:
        c.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER")

    # ── Миграция chat_history: изоляция диалога по веткам (conv) ──
    c.execute("PRAGMA table_info(chat_history)")
    hist_cols = {row[1] for row in c.fetchall()}
    if "conv" not in hist_cols:
        c.execute("ALTER TABLE chat_history ADD COLUMN conv TEXT")
        # Старая история была общей по user_id → переносим в личку этого пользователя
        c.execute("UPDATE chat_history SET conv='c'||user_id WHERE conv IS NULL")
        c.execute("CREATE INDEX IF NOT EXISTS idx_history_conv ON chat_history(conv, created_at)")
    conn.commit()
    conn.close()
    logger.info("Database ready ✓ (v6.0 schema)")


def db_add_task(title, description=None, due_date=None, due_time=None,
                priority="medium", period="day", energy_type=None, project_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title,description,due_date,due_time,priority,period,energy_type,project_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (title, description, due_date, due_time, priority, period, energy_type, project_id)
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


def db_tasks_completed_on(date_str):
    """Задачи, завершённые в указанный день (для разбора план/факт)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id,title,priority,energy_type,project_id FROM tasks "
        "WHERE status='completed' AND date(completed_at)=?",
        (date_str,)
    )
    rows = c.fetchall(); conn.close()
    return rows


# ── Дедупликация задач ──
_TASK_STOPWORDS = {
    "написать", "сделать", "отправить", "позвонить", "подготовить", "добавить",
    "проверить", "закрыть", "сделай", "напиши", "отправь", "по", "для", "на",
    "в", "и", "с", "до", "к", "нужно", "надо",
}

def _norm_title(s: str) -> str:
    """Нормализуем заголовок для сравнения дублей: нижний регистр, без знаков и стоп-слов."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    words = [w for w in s.split() if w not in _TASK_STOPWORDS]
    return " ".join(words)

def db_find_duplicate_task(title: str):
    """Вернёт (id, title) активной задачи с тем же нормализованным смыслом, иначе None."""
    key = _norm_title(title)
    if not key:
        return None
    for r in db_list_tasks(status="pending"):
        if _norm_title(r[1]) == key:
            return (r[0], r[1])
    return None

def db_dedup_pending_tasks() -> int:
    """Схлопывает дубли среди активных задач (оставляет первую — по приоритету/дате). Возвращает число удалённых."""
    rows = db_list_tasks(status="pending")  # уже отсортированы: high→low, due_date ASC
    seen, to_delete = set(), []
    for r in rows:
        key = _norm_title(r[1])
        if not key:
            continue
        if key in seen:
            to_delete.append(r[0])
        else:
            seen.add(key)
    if to_delete:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.executemany("DELETE FROM tasks WHERE id=?", [(i,) for i in to_delete])
        conn.commit(); conn.close()
    return len(to_delete)


# ── Период-ключи ──
def period_keys(now: datetime):
    iso = now.isocalendar()
    return {
        "month": now.strftime("%Y-%m"),
        "week":  f"{iso[0]}-W{iso[1]:02d}",
        "day":   now.strftime("%Y-%m-%d"),
    }


# ── Цели и фокус (goals) ──
def db_set_goal(scope, text, period_key, single=False):
    """single=True — заменить существующую цель этого scope+period (для фокуса дня / узкого места)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if single:
        c.execute("UPDATE goals SET status='archived' WHERE scope=? AND period_key=? AND status='active'",
                  (scope, period_key))
    c.execute("INSERT INTO goals (scope,text,period_key,status) VALUES (?,?,?,'active')",
              (scope, text, period_key))
    gid = c.lastrowid
    conn.commit(); conn.close()
    return gid

def db_list_goals(scope=None, period_key=None, status="active"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    q, p = "SELECT id,scope,text,period_key,status FROM goals WHERE status=?", [status]
    if scope:      q += " AND scope=?";      p.append(scope)
    if period_key: q += " AND period_key=?"; p.append(period_key)
    q += " ORDER BY id DESC"
    c.execute(q, p); rows = c.fetchall(); conn.close()
    return rows

def db_complete_goal(goal_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE goals SET status='done' WHERE id=?", (goal_id,))
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


# ── Проекты + рейтинг (рычаг) ──
def _project_score(profit, prob, time_req, strategic):
    """Рычаг = ожид.прибыль(норм) × вероятность × стратегичность / время. 0..100."""
    try:
        prob = (prob or 50) / 100.0
        strategic = (strategic or 3)            # 1..5
        time_req = max(time_req or 3, 1)         # 1..5 (1=быстро, 5=долго)
        profit = max(profit or 0, 0)
        # нормируем прибыль логарифмически чтобы не доминировала
        import math
        profit_n = math.log10(profit + 1) / 7.0  # ~0..1 при прибыли до 10 млн
        raw = profit_n * prob * (strategic / 5.0) / (time_req / 5.0)
        return round(min(raw * 100, 100), 1)
    except Exception:
        return 0.0

def db_add_project(name, expected_profit=None, success_prob=None,
                   time_required=None, strategic_value=None, comment=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO projects (name,expected_profit,success_prob,time_required,strategic_value,comment) "
        "VALUES (?,?,?,?,?,?)",
        (name, expected_profit, success_prob, time_required, strategic_value, comment)
    )
    pid = c.lastrowid; conn.commit(); conn.close()
    return pid

def db_update_project(project_id, **kwargs):
    if not kwargs: return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    fields = ", ".join(f"{k}=?" for k in kwargs)
    c.execute(f"UPDATE projects SET {fields} WHERE id=?", list(kwargs.values()) + [project_id])
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok

def db_list_projects(status="active"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if status == "all":
        c.execute("SELECT id,name,status,expected_profit,success_prob,time_required,strategic_value,comment FROM projects")
    else:
        c.execute("SELECT id,name,status,expected_profit,success_prob,time_required,strategic_value,comment FROM projects WHERE status=?", (status,))
    rows = c.fetchall(); conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "name": r[1], "status": r[2],
            "expected_profit": r[3], "success_prob": r[4],
            "time_required": r[5], "strategic_value": r[6], "comment": r[7],
            "score": _project_score(r[3], r[4], r[5], r[6]),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ── Идеи (второй мозг) ──
def db_add_idea(text, category="other"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO ideas (text,category) VALUES (?,?)", (text, category))
    iid = c.lastrowid; conn.commit(); conn.close()
    return iid

def db_list_ideas(status="new", category=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    q, p = "SELECT id,text,category,status,created_at FROM ideas WHERE 1=1", []
    if status and status != "all": q += " AND status=?";   p.append(status)
    if category:                   q += " AND category=?"; p.append(category)
    q += " ORDER BY id DESC"
    c.execute(q, p); rows = c.fetchall(); conn.close()
    return rows

def db_update_idea(idea_id, **kwargs):
    if not kwargs: return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    fields = ", ".join(f"{k}=?" for k in kwargs)
    c.execute(f"UPDATE ideas SET {fields} WHERE id=?", list(kwargs.values()) + [idea_id])
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


# ── Ожидания от других ──
def db_add_waiting(what, who=None, due_date=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO waiting_for (what,who,due_date) VALUES (?,?,?)", (what, who, due_date))
    wid = c.lastrowid; conn.commit(); conn.close()
    return wid

def db_list_waiting(status="waiting"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if status == "all":
        c.execute("SELECT id,what,who,due_date,status FROM waiting_for ORDER BY id DESC")
    else:
        c.execute("SELECT id,what,who,due_date,status FROM waiting_for WHERE status=? ORDER BY id DESC", (status,))
    rows = c.fetchall(); conn.close()
    return rows

def db_resolve_waiting(waiting_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE waiting_for SET status='done' WHERE id=?", (waiting_id,))
    ok = c.rowcount > 0; conn.commit(); conn.close()
    return ok


def conv_key(chat_id, thread_id=None) -> str:
    """Ключ изоляции диалога: личка и каждая ветка-тема — отдельный контекст."""
    return f"c{chat_id}:t{thread_id}" if thread_id else f"c{chat_id}"


def db_get_history(conv, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role,content FROM chat_history WHERE conv=? ORDER BY created_at DESC, id DESC LIMIT ?",
        (str(conv), limit)
    )
    rows = c.fetchall(); conn.close()
    return list(reversed(rows))


def db_save_message(conv, role, content):
    conv = str(conv)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO chat_history (conv,role,content) VALUES (?,?,?)", (conv, role, content))
    c.execute(
        "DELETE FROM chat_history WHERE conv=? AND id NOT IN "
        "(SELECT id FROM chat_history WHERE conv=? ORDER BY created_at DESC, id DESC LIMIT 60)",
        (conv, conv)
    )
    conn.commit(); conn.close()


def db_clear_history(conv):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM chat_history WHERE conv=?", (str(conv),))
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


def db_reminders_sent_count(event_id: str) -> int:
    """Сколько напоминаний уже отправлено для данного события."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sent_reminders WHERE event_id=?", (event_id,))
    count = c.fetchone()[0]
    conn.close()
    return count


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
def _load_google_creds():
    """
    Загружает creds из token.pickle и при необходимости обновляет access-токен.
    Если в pickle нет полей для refresh (client_id/secret/token_uri) — дополняет их
    из переменных окружения (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET). Это лечит ошибку
    'credentials do not contain the necessary fields need to refresh the access token'
    без перегенерации токена (если refresh_token в pickle присутствует).
    """
    if not os.path.exists(GOOGLE_TOKEN_FILE):
        return None
    with open(GOOGLE_TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)
    if not creds:
        return None

    # Дополняем недостающие поля для refresh из окружения
    needs_fields = (not getattr(creds, "client_id", None)
                    or not getattr(creds, "client_secret", None)
                    or not getattr(creds, "token_uri", None))
    if needs_fields and getattr(creds, "refresh_token", None) and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=getattr(creds, "token", None),
            refresh_token=creds.refresh_token,
            token_uri=getattr(creds, "token_uri", None) or GOOGLE_TOKEN_URI,
            client_id=getattr(creds, "client_id", None) or GOOGLE_CLIENT_ID,
            client_secret=getattr(creds, "client_secret", None) or GOOGLE_CLIENT_SECRET,
            scopes=getattr(creds, "scopes", None),
        )
        logger.info("Google creds дополнены client_id/secret из окружения")

    # Refresh если: истёк, невалиден, или expiry=None (токен без срока — всегда обновляем)
    if creds.refresh_token and (not creds.valid or creds.expired or creds.expiry is None):
        creds.refresh(Request())
        with open(GOOGLE_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
        logger.info("Google token refreshed ✓")
    return creds


def get_calendar_service():
    try:
        creds = _load_google_creds()
        if not creds:
            return None
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Calendar auth error: {e}")
        return None


def _to_rfc3339(dt: datetime) -> str:
    """naive datetime (MSK) → RFC3339 с timezone offset"""
    return dt.replace(tzinfo=TZ).isoformat()


def _parse_user_dt(s: str) -> datetime:
    """Толерантный разбор даты-времени из разных форматов модели/пользователя."""
    s = (s or "").strip().replace("T", " ")
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m %H:%M"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            if f == "%d.%m %H:%M":  # год не указан — берём текущий
                dt = dt.replace(year=datetime.now(TZ).year)
            return dt
        except ValueError:
            continue
    from dateutil.parser import parse as dtparse
    return dtparse(s, dayfirst=True)  # последний шанс


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
    try:
        creds = _load_google_creds()
        if not creds:
            return None
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


def gtasks_delete(task_id: str):
    service = get_tasks_service()
    if not service:
        return False, "Google Tasks не подключён"
    try:
        service.tasks().delete(tasklist="@default", taskId=task_id).execute()
        logger.info(f"Google Task удалена: {task_id}")
        return True, None
    except Exception as e:
        logger.error(f"gtasks_delete: {e}")
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
# ─── Финансовый контур: KPI из task-board ───────────────────────────────────────
_KPI_CACHE = {"data": None, "ts": 0.0}

def fetch_business_kpi(force: bool = False) -> dict:
    """
    Тянет бизнес-метрики из task-board (GET /api/kpi).
    Те же цифры, что видит Лариса. Кэш 5 минут, чтобы не дёргать API на каждом запросе.
    """
    if not FINANCE_API_URL or not FINANCE_API_TOKEN:
        return {"ok": False, "error": "Финансовый контур не настроен"}
    now = time.time()
    if not force and _KPI_CACHE["data"] and (now - _KPI_CACHE["ts"] < 300):
        return _KPI_CACHE["data"]
    try:
        req = urllib.request.Request(
            FINANCE_API_URL,
            headers={"Authorization": f"Bearer {FINANCE_API_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = {"ok": True, **data}
        _KPI_CACHE["data"] = result
        _KPI_CACHE["ts"]   = now
        logger.info("KPI обновлены из финансовой системы ✓")
        return result
    except urllib.error.HTTPError as e:
        logger.warning(f"fetch_business_kpi HTTP {e.code}")
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        logger.warning(f"fetch_business_kpi: {e}")
        return {"ok": False, "error": str(e)}


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
            start_dt = _parse_user_dt(inp["start_datetime"])
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
            end_dt = (_parse_user_dt(inp["end_datetime"]) if inp.get("end_datetime") else None)
            eid, err = calendar_add_event(inp["title"], start_dt, end_dt, inp.get("description"))
            if eid:
                return {"ok": True, "event_id": eid, "title": inp["title"], "start": inp["start_datetime"]}
            return {"ok": False, "error": err or "не удалось создать событие"}
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
            start_dt = _parse_user_dt(inp["start_datetime"]) if inp.get("start_datetime") else None
            end_dt   = _parse_user_dt(inp["end_datetime"])   if inp.get("end_datetime")   else None
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
        # Дедуп по смыслу (нормализованный заголовок) ПЕРЕД добавлением
        new_key = _norm_title(inp["title"])
        # 1) локальная база задач
        local_dup = db_find_duplicate_task(inp["title"])
        if local_dup:
            return {"ok": False, "duplicate": True,
                    "message": f"Похожая задача уже есть: #{local_dup[0]} «{local_dup[1]}». Дубль не добавляю."}
        # 2) Google Tasks
        existing, _ = gtasks_list(show_completed=False)
        for t in existing:
            if _norm_title(t.get("title", "")) == new_key and new_key:
                return {"ok": False, "duplicate": True,
                        "message": f"Задача «{inp['title']}» по смыслу уже есть в Google Tasks — дубль не добавляю."}
        # Если due_date не передан — ставим сегодня (иначе задача не видна в календаре)
        due_date = inp.get("due_date") or str(now.date())
        gid, err = gtasks_add(
            title=inp["title"],
            due_date=due_date,
            due_time=inp.get("due_time"),
            notes=inp.get("notes"),
        )
        if gid:
            db_add_task(
                title=inp["title"],
                description=inp.get("notes"),
                due_date=due_date,
                due_time=inp.get("due_time"),
                priority=inp.get("priority", "medium"),
                period=inp.get("period", "day"),
                energy_type=inp.get("energy_type"),
                project_id=inp.get("project_id"),
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

    elif name == "delete_google_task":
        ok, err = gtasks_delete(inp["task_id"])
        return {"ok": ok, "error": err}

    elif name == "get_business_kpi":
        return fetch_business_kpi(force=inp.get("force", False))

    # ── Цели и фокус ──
    elif name == "set_goal":
        scope = inp["scope"]  # month | week | day_focus | bottleneck
        keys = period_keys(now)
        pk = (inp.get("period_key")
              or (keys["month"] if scope == "month"
                  else keys["week"] if scope == "week"
                  else keys["day"]))
        single = scope in ("day_focus", "bottleneck")
        gid = db_set_goal(scope, inp["text"], pk, single=single)
        return {"ok": True, "goal_id": gid, "scope": scope, "period_key": pk}

    elif name == "list_goals":
        rows = db_list_goals(scope=inp.get("scope"), period_key=inp.get("period_key"),
                             status=inp.get("status", "active"))
        return {"goals": [{"id": r[0], "scope": r[1], "text": r[2],
                           "period_key": r[3], "status": r[4]} for r in rows]}

    elif name == "complete_goal":
        return {"ok": db_complete_goal(inp["goal_id"])}

    # ── Проекты + рейтинг ──
    elif name == "add_project":
        pid = db_add_project(
            name=inp["name"],
            expected_profit=inp.get("expected_profit"),
            success_prob=inp.get("success_prob"),
            time_required=inp.get("time_required"),
            strategic_value=inp.get("strategic_value"),
            comment=inp.get("comment"),
        )
        return {"ok": True, "project_id": pid}

    elif name == "update_project":
        pid = inp.pop("project_id")
        return {"ok": db_update_project(pid, **inp) if inp else False}

    elif name == "list_projects":
        return {"projects": db_list_projects(status=inp.get("status", "active"))}

    # ── Идеи (второй мозг) ──
    elif name == "add_idea":
        iid = db_add_idea(inp["text"], inp.get("category", "other"))
        return {"ok": True, "idea_id": iid}

    elif name == "list_ideas":
        rows = db_list_ideas(status=inp.get("status", "new"), category=inp.get("category"))
        return {"ideas": [{"id": r[0], "text": r[1], "category": r[2],
                           "status": r[3], "created_at": r[4]} for r in rows]}

    elif name == "update_idea":
        iid = inp.pop("idea_id")
        return {"ok": db_update_idea(iid, **inp) if inp else False}

    # ── Ожидания от других ──
    elif name == "add_waiting":
        wid = db_add_waiting(inp["what"], inp.get("who"), inp.get("due_date"))
        return {"ok": True, "waiting_id": wid}

    elif name == "list_waiting":
        rows = db_list_waiting(status=inp.get("status", "waiting"))
        return {"waiting": [{"id": r[0], "what": r[1], "who": r[2],
                             "due_date": r[3], "status": r[4]} for r in rows]}

    elif name == "resolve_waiting":
        return {"ok": db_resolve_waiting(inp["waiting_id"])}

    elif name == "dedup_tasks":
        # Чистим дубли: локальная база + Google Tasks (по смыслу заголовка)
        local_removed = db_dedup_pending_tasks()
        google_removed = 0
        items, err = gtasks_list(show_completed=False)
        if not err and items:
            seen = set()
            for t in items:
                key = _norm_title(t.get("title", ""))
                if not key:
                    continue
                if key in seen:
                    ok, _ = gtasks_delete(t.get("id"))
                    if ok:
                        google_removed += 1
                else:
                    seen.add(key)
        return {"ok": True, "local_removed": local_removed, "google_removed": google_removed}

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
         "energy_type": {"type": "string", "enum": ["deep", "comm", "ops", "creative", "routine"],
                         "description": "Тип нагрузки: deep=глубокая работа, comm=коммуникация/созвоны, ops=операционка, creative=креатив, routine=рутина"},
         "project_id":  {"type": "integer", "description": "ID проекта (из list_projects), если задача относится к проекту"},
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
    {"name": "delete_google_task",
     "description": "Удалить задачу из Google Tasks по ID. Вызови list_google_tasks чтобы найти ID.",
     "parameters": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string", "description": "ID задачи из list_google_tasks"},
     }}},
    {"name": "get_business_kpi",
     "description": "Получить бизнес-метрики ALTA из финансовой системы (раздел «Экономика»): "
                    "поступления за месяц/год, суммы подписанных договоров, дебиторку (всю и просроченную), "
                    "потенциал пресейл-воронки, количество лидов, КП на выставлении, назначенные встречи, расходы за месяц. "
                    "Вызывай когда Глеб спрашивает про деньги, продажи, выручку, кассу, лиды, долги клиентов — "
                    "или когда нужно понять, что реально двигает бизнес и где узкое место.",
     "parameters": {"type": "object", "properties": {
         "force": {"type": "boolean", "description": "Принудительно обновить, минуя кэш (по умолчанию false)"},
     }}},

    # ── Цели и фокус ──
    {"name": "set_goal",
     "description": "Задать цель или фокус. scope: 'month' — цель месяца, 'week' — цель недели, "
                    "'day_focus' — главный фокус дня (одна фраза), 'bottleneck' — узкое место бизнеса сейчас "
                    "(что СИЛЬНЕЕ ВСЕГО ограничивает рост — только одно). Для day_focus и bottleneck новая запись "
                    "заменяет предыдущую за тот же день.",
     "parameters": {"type": "object", "required": ["scope", "text"], "properties": {
         "scope": {"type": "string", "enum": ["month", "week", "day_focus", "bottleneck"]},
         "text":  {"type": "string"},
     }}},
    {"name": "list_goals",
     "description": "Получить активные цели/фокус/узкое место. Без scope — все.",
     "parameters": {"type": "object", "properties": {
         "scope":  {"type": "string", "enum": ["month", "week", "day_focus", "bottleneck"]},
         "status": {"type": "string", "enum": ["active", "done", "archived"]},
     }}},
    {"name": "complete_goal",
     "description": "Отметить цель достигнутой.",
     "parameters": {"type": "object", "required": ["goal_id"], "properties": {
         "goal_id": {"type": "integer"},
     }}},

    # ── Проекты + рейтинг ──
    {"name": "add_project",
     "description": "Добавить проект для оценки рычага. Оценки нужны чтобы понять, какой проект приносит "
                    "максимальный эффект на вложенное время.",
     "parameters": {"type": "object", "required": ["name"], "properties": {
         "name":            {"type": "string"},
         "expected_profit": {"type": "integer", "description": "Ожидаемая прибыль в ₽"},
         "success_prob":    {"type": "integer", "description": "Вероятность успеха, 0–100"},
         "time_required":   {"type": "integer", "description": "Сколько времени требует, 1 (быстро) – 5 (долго)"},
         "strategic_value": {"type": "integer", "description": "Стратегическая ценность, 1–5"},
         "comment":         {"type": "string"},
     }}},
    {"name": "update_project",
     "description": "Обновить проект (оценки, статус, комментарий). status: active|paused|done|dropped.",
     "parameters": {"type": "object", "required": ["project_id"], "properties": {
         "project_id":      {"type": "integer"},
         "name":            {"type": "string"},
         "status":          {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
         "expected_profit": {"type": "integer"},
         "success_prob":    {"type": "integer"},
         "time_required":   {"type": "integer"},
         "strategic_value": {"type": "integer"},
         "comment":         {"type": "string"},
     }}},
    {"name": "list_projects",
     "description": "Список проектов с рассчитанным рейтингом рычага (score, по убыванию). "
                    "Используй чтобы сказать какой проект сейчас даёт наибольший рычаг.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["active", "paused", "done", "dropped", "all"]},
     }}},

    # ── Идеи (второй мозг) ──
    {"name": "add_idea",
     "description": "Сохранить идею в банк идей. Идея — это гипотеза, не задача. Не превращай идеи в задачи "
                    "автоматически. category: content|product|partnership|automation|other.",
     "parameters": {"type": "object", "required": ["text"], "properties": {
         "text":     {"type": "string"},
         "category": {"type": "string", "enum": ["content", "product", "partnership", "automation", "other"]},
     }}},
    {"name": "list_ideas",
     "description": "Получить идеи из банка. status: new|promising|archived|all.",
     "parameters": {"type": "object", "properties": {
         "status":   {"type": "string", "enum": ["new", "promising", "archived", "all"]},
         "category": {"type": "string", "enum": ["content", "product", "partnership", "automation", "other"]},
     }}},
    {"name": "update_idea",
     "description": "Обновить идею (например пометить promising или archived).",
     "parameters": {"type": "object", "required": ["idea_id"], "properties": {
         "idea_id":  {"type": "integer"},
         "text":     {"type": "string"},
         "category": {"type": "string", "enum": ["content", "product", "partnership", "automation", "other"]},
         "status":   {"type": "string", "enum": ["new", "promising", "archived"]},
     }}},

    # ── Ожидания от других ──
    {"name": "add_waiting",
     "description": "Зафиксировать ожидание от другого человека (кто-то должен что-то прислать/ответить/сделать).",
     "parameters": {"type": "object", "required": ["what"], "properties": {
         "what":     {"type": "string", "description": "Что ждём"},
         "who":      {"type": "string", "description": "От кого"},
         "due_date": {"type": "string", "description": "YYYY-MM-DD — к какому сроку (если есть)"},
     }}},
    {"name": "list_waiting",
     "description": "Список ожиданий от других людей. status: waiting|done|all.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["waiting", "done", "all"]},
     }}},
    {"name": "resolve_waiting",
     "description": "Закрыть ожидание (получили то, что ждали).",
     "parameters": {"type": "object", "required": ["waiting_id"], "properties": {
         "waiting_id": {"type": "integer"},
     }}},
    {"name": "dedup_tasks",
     "description": "Удалить дубли задач (по смыслу заголовка) — и в локальной базе, и в Google Tasks. "
                    "Вызывай, когда в списке видны повторяющиеся задачи. Возвращает сколько удалено.",
     "parameters": {"type": "object", "properties": {}}},
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

    # Бизнес-метрики из финансовой системы (раздел «Экономика»)
    def _money(n):
        try:
            return f"{int(n):,}".replace(",", " ")
        except Exception:
            return str(n)
    kpi = fetch_business_kpi()
    if kpi.get("ok"):
        kpi_block = (
            f"Поступило за месяц: {_money(kpi.get('received_this_month', 0))} ₽  |  за год: {_money(kpi.get('received_this_year', 0))} ₽\n"
            f"Дебиторка: {_money(kpi.get('receivables_total', 0))} ₽ (просрочено {_money(kpi.get('receivables_overdue', 0))} ₽)\n"
            f"Пресейл: {kpi.get('presale_leads', 0)} лидов, КП на выставлении: {kpi.get('kp_outstanding', 0)}, встреч назначено: {kpi.get('meetings_scheduled', 0)}\n"
            f"Расходы за месяц: {_money(kpi.get('expenses_this_month', 0))} ₽"
        )
    else:
        kpi_block = "  (финансовые данные сейчас недоступны)"

    # ── Цели, фокус, узкое место ──
    keys = period_keys(now)
    month_goals = db_list_goals(scope="month",     period_key=keys["month"])
    week_goals  = db_list_goals(scope="week",      period_key=keys["week"])
    focus_rows  = db_list_goals(scope="day_focus", period_key=keys["day"])
    bn_rows     = db_list_goals(scope="bottleneck", period_key=keys["day"])
    def _goals_text(rows):
        return "\n".join(f"  • {r[2]}" for r in rows) if rows else "  (не заданы)"
    month_block     = _goals_text(month_goals)
    week_block      = _goals_text(week_goals)
    focus_text      = focus_rows[0][2] if focus_rows else "(не задан)"
    bottleneck_text = bn_rows[0][2] if bn_rows else "(не определено)"

    # ── Энергобаланс задач на сегодня ──
    energy_labels = {"deep": "глубокая работа", "comm": "коммуникация",
                     "ops": "операционка", "creative": "креатив", "routine": "рутина"}
    today_str = keys["day"]
    energy_counts = {}
    for r in pending:
        et = r[10] if len(r) > 10 else None
        is_today = (r[3] == today_str) or (r[3] is None and r[7] == "day")
        if et and is_today:
            energy_counts[et] = energy_counts.get(et, 0) + 1
    energy_block = (", ".join(f"{energy_labels.get(k, k)}: {v}" for k, v in energy_counts.items())
                    if energy_counts else "(тип нагрузки у задач не проставлен)")

    # ── Ожидания от других ──
    waiting = db_list_waiting(status="waiting")
    if waiting:
        waiting_block = "\n".join(
            f"  • {w[1]}" + (f" — от {w[2]}" if w[2] else "") + (f" (к {w[3]})" if w[3] else "")
            for w in waiting[:10]
        )
    else:
        waiting_block = "  (нет)"

    return f"""Ты — Семён, личный бизнес-ассистент Глеба.
Ты не чат-бот и не планировщик задач. Ты выполняешь роль сильного ассистента руководителя —
ближе к операционному директору, чем к секретарю. Знаешь проекты, цели, людей, договорённости,
обязательства и контекст бизнеса. Тебе не нужно показывать свою работу — Глебу нужен результат.

━━━ ТОН ━━━
Говоришь спокойно, по-человечески, уверенно. Коротко и по делу.
Если можно сказать в одном предложении — говоришь в одном предложении.
Если ситуация требует подробностей — объясняешь подробно.
Не перегружаешь сообщениями. Если всё хорошо — не мешаешь. Если видишь риск — говоришь прямо.

НИКОГДА не пиши канцелярит и шаблоны нейросетей. Запрещённые фразы:
«Я проанализировал», «На основе ваших данных», «Вот структурированный отчёт»,
«Резюмируя вышесказанное», «Ниже представлен», «Обнаружено превышение», «Выявлено несоответствие».
Вместо «На сегодня сформирован список задач» → «На сегодня три главные вещи».
Вместо «Обнаружено превышение нагрузки» → «На день 17 задач. Ты это не вывезешь, убери минимум пять».
Эмодзи — умеренно, как маркеры. Без «актёрской игры» и звёздочек с действиями.

━━━ ГЛАВНЫЙ ПРИНЦИП ━━━
Ты не управляешь задачами. Ты помогаешь Глебу принимать правильные решения каждый день.
Главное — деньги, клиенты, обязательства, репутация, стратегические цели и энергия владельца.
Не каждая задача важна. Не каждая срочная вещь требует внимания.
Если Глеб распыляется или уходит в операционку, забывая про продажи и развитие — ты обязан это показать.
Ты не споришь ради спора. Но если видишь ошибку, перегруз или потерю фокуса — говоришь прямо.

━━━ ТОЧНОЕ ВРЕМЯ ━━━
Сейчас: {now.strftime('%H:%M')} МСК | {day_names[today.weekday()]} {now.strftime('%d.%m.%Y')}
Сегодня: {today} | Завтра: {tomorrow}

Про прошлое и будущее: если сейчас {now.strftime('%H:%M')}, то всё что было ДО {now.strftime('%H:%M')} — уже прошло.
Никогда не называй прошедшее событие "предстоящим" или "впереди".

━━━ ЦЕЛИ И ФОКУС ━━━
Цель(и) месяца:
{month_block}
Цель(и) недели:
{week_block}
Фокус дня: {focus_text}

Каждая задача дня должна работать на цель недели/месяца. Если задача не в фокусе — мягко подсвети это.
Чтобы задать/изменить: set_goal (scope month|week|day_focus|bottleneck).

━━━ АКТИВНЫЕ ЗАДАЧИ (актуально на {now.strftime('%H:%M')}) ━━━
{tasks_block}

Перед добавлением задачи — проверь список выше. Если задача с таким смыслом уже есть → НЕ добавляй дубль, скажи что уже есть (#ID).

━━━ БИЗНЕС-МЕТРИКИ ALTA (раздел «Экономика») ━━━
{kpi_block}

Это живые цифры из финансовой системы. Используй их, когда речь о деньгах, продажах, кассе, лидах или долгах.
Когда Глеб распыляется на операционку — напоминай, что реально двигает бизнес (продажи, лиды, дебиторка).
За свежими/детальными данными вызывай get_business_kpi. Цифры не выдумывай — бери только отсюда или из инструмента.

━━━ УЗКОЕ МЕСТО (что сильнее всего ограничивает рост сейчас) ━━━
{bottleneck_text}

Узкое место — всегда ОДНО. Например: нет лидов, нет продаж, кассовый разрыв, перегруз Глеба, нет менеджера, нехватка контента.
Все твои рекомендации по приоритетам строй вокруг узкого места. Если оно не определено и у тебя есть данные — предложи определить (set_goal scope=bottleneck).

━━━ ЭНЕРГОБАЛАНС НА СЕГОДНЯ ━━━
{energy_block}

Люди работают не задачами, а энергией. Типы нагрузки: глубокая работа / коммуникация / операционка / креатив / рутина.
Если на день перекос (например 8 часов коммуникаций и 0 глубокой работы) — скажи об этом.
Проставляй energy_type при добавлении значимых задач.

━━━ ОЖИДАНИЯ ОТ ДРУГИХ ━━━
{waiting_block}

Кто кому что должен прислать/ответить. Фиксируй через add_waiting, закрывай через resolve_waiting.
Если срок прошёл — напомни прямо: «Павел всё ещё ждёт КП» / «Рома не прислал варианты с понедельника».

━━━ ЗАДАЧА vs СОБЫТИЕ — ЖЕЛЕЗНОЕ ПРАВИЛО ━━━
📅 СОБЫТИЕ = Глеб явно назвал время ("встреча в 15:00", "созвон завтра в 11:30")
   → add_calendar_event. Только одно действие.

📋 ЗАДАЧА = всё остальное — дела, пункты списка, поручения БЕЗ конкретного времени
   → add_google_task с сегодняшней датой. Только одно действие.

🚫 НИКОГДА не создавай "рабочие блоки" в календаре — "10:30-12:00 работа над задачами X,Y,Z" — это НЕ событие, это задачи. Добавляй их как задачи, не как события.
🚫 НИКОГДА не добавляй одно и то же и в задачи, и в календарь.
🚫 НИКОГДА не выдумывай блоки и временные слоты которые Глеб не просил.

━━━ РАБОТА С ЗАДАЧАМИ ━━━
• add_google_task — всегда передавай due_date (сегодня по умолчанию). Без даты задача не видна в календаре.
• Перед добавлением задачи: если похожая уже есть в системе — НЕ добавляй, скажи об этом.
• Для удаления задачи: сначала list_google_tasks (найти ID), потом delete_google_task.
• Если видишь в списке повторяющиеся задачи — вызови dedup_tasks (или предложи Глебу /dedup). Не плоди дубли.
• «написать КП», «отправить КП Павлу», «КП Павлу» — это ОДНА задача, не три.

━━━ ПРАВИЛА ДЕЙСТВИЙ ━━━
1. Задача без времени → add_google_task(title, due_date=сегодня)
2. Событие с временем → add_calendar_event(title, start_datetime)
3. "Сегодня" / "план дня" → get_daily_summary с датой {today}
4. "Завтра" → get_daily_summary с датой {tomorrow}
5. "Неделя" → get_weekly_summary
6. Удалить задачу → list_google_tasks → delete_google_task по id
7. Удалить событие → get_calendar_events → delete_calendar_event по id
8. Перенести событие → get_calendar_events → update_calendar_event
9. Голосовые автотранскрибируются — отвечай на суть.
10. get_calendar_events — только если Глеб явно просит показать календарь.

━━━ ЗАПРЕТЫ ━━━
🚫 НЕ добавляй в конце сообщений фразы типа "кстати, через X минут у тебя Y" — даже если видишь это в данных календаря. Напоминания о времени рассылает только автопланировщик.
🚫 НЕ называй прошедшее событие предстоящим.
🚫 НЕ придумывай встречи/события/дедлайны которых нет в данных API.
✅ Показывай события и время — только когда Глеб сам спрашивает ("что сегодня?", "план дня", "сколько до встречи?").

━━━ РАЗБОР СУЩНОСТЕЙ — что куда ━━━
Не всё, что говорит Глеб — задача. Сначала классифицируй:
• Конкретное действие с результатом → задача (add_google_task).
• Время названо → событие (add_calendar_event).
• Гипотеза, «можно попробовать», «а что если» → идея (add_idea), НЕ задача.
• «Надо бы», «когда-нибудь» → идея или заметка, не задача.
• Кто-то другой должен что-то сделать/прислать → ожидание (add_waiting).
• Большое направление с прибылью/сроком → проект (add_project).
При разборе голосового/хаотичного сообщения отвечай по-человечески:
«Поймал. Вижу две задачи, один созвон и один вопрос — по времени созвона уточни».
Не пиши «Создано 3 задачи. Выявлено 4 сущности».

━━━ ИДЕИ (второй мозг) ━━━
Идеи Глеба — часто источник денег. Складывай их в банк идей, не теряй.
Категории: контент / продукт / партнёрство / автоматизация / другое.
Раз в неделю делаешь обзор: «За неделю 14 идей. Вот 3 самые перспективные».

━━━ ПРОЕКТЫ И РЫЧАГ ━━━
Не все проекты равны. У каждого: ожидаемая прибыль, вероятность, требуемое время, стратегическая ценность.
list_projects даёт рейтинг по рычагу (score). Используй, чтобы сказать прямо:
«Из активных проектов наибольший рычаг сейчас у X — туда и стоит вкладывать время».

━━━ КОГДА СПОРИТЬ (твоя главная ценность) ━━━
Перегруз: «На завтра 14 задач. Реально успеешь примерно половину».
Потеря фокуса: «Последние три дня почти всё ушло в операционку. Продажами ты не занимался».
Несоответствие цели: «Ты говорил, цель недели — продажи. Но большинство задач не про это».
Риск: «Если сегодня не выставить счёт, деньги сдвинутся минимум на неделю».
Возможность: «Сейчас самое выгодное вложение времени — обработать новые лиды. Остальное подождёт».
Нет денежных задач: «Сегодня нет ни одной задачи, которая приносит деньги».
Говори это прямо, спокойно, без драматизации. Один раз, не долби.

━━━ ПРИОРИТЕТЫ ━━━
🔴 high — горит | 🟡 medium — важно, не срочно | 🟢 low — когда-нибудь

━━━ ПЕРИОДЫ ЗАДАЧ ━━━
day — сегодня | week — эта неделя | month — этот месяц

━━━ ИСТОРИЯ ДИАЛОГА ━━━
История — это контекст того что уже было сделано. Не очередь команд.
Отвечай ТОЛЬКО на последнее сообщение Глеба.
НЕ добавляй задачи/события из предыдущих сообщений истории — они уже обработаны.
Если задача из истории уже есть в списке активных выше — не добавляй снова.

━━━ ТЕХНИЧЕСКИЕ ОШИБКИ ━━━
Если инструмент вернул ошибку (календарь, задачи) — просто скажи что не получилось выполнить действие.
НЕ ставь диагноз ("требуется переавторизация", "токен истёк", "API сбой").
НЕ обещай "синхронизировать позже" — ты не умеешь делать отложенные действия.
НЕ говори "держу в оперативной памяти" — у тебя нет памяти между сессиями.
Если calendar/tasks недоступны — честно скажи "не смог сохранить" и предложи попробовать снова.

━━━ ФОРМАТИРОВАНИЕ ━━━
Ответы пиши в обычном тексте с минимальным форматированием.
Жирный текст: **слово** (умеренно, для важного). Маркеры списков — только «•».
При показе сводок указывай прошедшие события как "(было)" рядом с временем.

🚫 НИКОГДА не печатай в ответе вызовы инструментов и их имена: «add_idea(...)», «set_goal(...)»,
«Идеи сохранил: ...» и т.п. Инструменты выполняются НЕЗАМЕТНО. Если сохранил идею/задачу —
скажи это обычными словами одной короткой фразой («Идею записал»), без скобок и без кода.
🚫 НЕ используй markdown-заголовки (#, ##, ###) и горизонтальные линии (---). Telegram их не рендерит — будет мусор.
🚫 НЕ ставь служебные пометки в скобках вроде «(зафиксировал в банке)».
🚫 НЕ используй «*» или «-» как маркеры — только «•»."""


# ─── AI Agent ─────────────────────────────────────────────────────────────────
async def process_with_gemini(conv, user_message: str, save_history: bool = True) -> str:
    """
    conv — ключ изоляции диалога (личка / конкретная ветка-тема).
    save_history=False используется для автопостинга —
    чтобы системные запросы не засоряли историю диалога.
    """
    conv = str(conv)
    history = db_get_history(conv, limit=20) if save_history else []
    logger.info(f"Семён: model={GEMINI_MODEL}, conv={conv}, history={len(history)}, save={save_history}")

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
            # Убираем AI-напоминания о времени — их отправляет планировщик, не ИИ
            final_text = filter_ai_reminders(final_text)
            # Чистим технический мусор (вызовы инструментов, служебные пометки, markdown)
            final_text = clean_ai_output(final_text)
            if save_history and final_text:
                db_save_message(conv, "user", user_message)
                db_save_message(conv, "assistant", final_text)
            return final_text or "Готово."

    return "Семён завис — попробуй ещё раз."


# ─── Отправка сообщений (с HTML и fallback) ───────────────────────────────────
_REMINDER_LEAK_RE = re.compile(
    r'через\s+\d+\s+минут\w*\s+встреч',
    re.IGNORECASE
)

async def send_html(bot_or_update, text: str, chat_id: int = None,
                    thread_id: int = None, reply_to=None):
    """Универсальная отправка: конвертируем Markdown → HTML, fallback в plain text."""
    # ЖЕЛЕЗНЫЙ БЛОК: если в тексте утекло AI-напоминание — не отправляем вообще
    if _REMINDER_LEAK_RE.search(text):
        logger.warning(f"BLOCKED reminder leak in send_html: {text[:80]!r}")
        return
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
        f"Утро. Составь план дня на {day_names[today.weekday()]} {today.strftime('%d.%m.%Y')}. "
        f"Вызови get_daily_summary (дата {today}). Учитывай цели/фокус, узкое место и KPI из системного контекста. "
        "Формат, по-человечески и коротко:\n"
        "🎯 Фокус дня — одна фраза\n"
        "Топ-3 задачи (только важное)\n"
        "📅 Встречи со временем (или «нет»)\n"
        "💰 Деньги — что сегодня приближает к выручке/закрывает дебиторку\n"
        "👥 Кого пнуть / что ждём от других\n"
        "⚠️ Риск дня (если есть)\n"
        "Без воды, без прогресс-баров.",
        save_history=False
    )
    return result


async def generate_week_plan(user_id: int) -> str:
    result = await process_with_gemini(
        user_id,
        "Понедельник. Составь план недели. Вызови get_weekly_summary и list_goals. "
        "Привяжи к цели месяца. Формат:\n"
        "🏆 Главная цель недели — одна фраза\n"
        "💰 Денежные задачи недели (что влияет на выручку)\n"
        "👤 Клиенты — кто ждёт, кому написать\n"
        "📅 Ключевые встречи\n"
        "⚠️ Риски недели\n"
        "БЕЗ прогресс-баров, БЕЗ процентов — только план.",
        save_history=False
    )
    return result


async def generate_evening_report(user_id: int) -> str:
    """
    Вечерняя сводка: итог дня + взгляд опытного предпринимателя-стратега.
    Семён фоново думает, как выйти на новый уровень (к BIG_GOAL), и предлагает
    мощные амбициозные идеи на завтра. Лучшие идеи сохраняет в банк идей.
    """
    today = date.today()
    result = await process_with_gemini(
        user_id,
        f"Вечер {today.strftime('%d.%m.%Y')}. Подведи итог дня и подумай как сильный предприниматель-стратег.\n"
        "Сначала вызови get_daily_summary (дата сегодня), list_waiting и get_business_kpi.\n\n"
        "Часть 1 — Итог дня (коротко, по-человечески):\n"
        "✅ Сделано\n"
        "❌ Не успели + почему\n"
        "➡️ Переносим на завтра\n"
        "🧠 Решения / 👀 что ждём от других\n\n"
        f"Часть 2 — Взгляд стратега со стороны (главное!). Цель — {BIG_GOAL}.\n"
        "Подумай амбициозно: что реально выведет бизнес на новый уровень, а не косметика. "
        "Опираясь на узкое место и KPI, предложи 2-3 мощные, смелые идеи/хода на завтра и ближайшее время — "
        "как опытный предприниматель, который видит картину со стороны. Каждая идея: суть + почему это рычаг к цели.\n"
        "Затем самые сильные 1-2 идеи сохрани через add_idea (подходящая category).\n\n"
        "🔥 Заверши одной фразой — главное на завтра.\n"
        "Тон живой и уверенный, без канцелярита. Не льсти, говори по делу.",
        save_history=False
    )
    return result


async def generate_idea_review(user_id: int) -> str:
    """Недельный обзор банка идей — выбрать самые перспективные."""
    result = await process_with_gemini(
        user_id,
        "Сделай недельный обзор банка идей. Вызови list_ideas (status new). "
        "Сгруппируй по категориям, посчитай сколько накопилось, и выбери 3 самые перспективные "
        f"с точки зрения движения к цели {BIG_GOAL}. По каждой — почему именно она. "
        "Самые сильные пометь через update_idea (status=promising). Коротко и по делу.",
        save_history=False
    )
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

            # Жёсткий лимит: если оба напоминания уже отправлены — пропускаем событие совсем
            if db_reminders_sent_count(event_id) >= 2:
                continue

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

        # Вечерняя стратегическая сводка: итог дня + амбициозные идеи на завтра
        if AUTO_EVENING_ENABLED:
            ev_h, ev_m = map(int, AUTO_EVENING_TIME.split(":"))
            ev_mins = ev_h * 60 + ev_m
            if abs(now_mins - ev_mins) <= 1 and not db_was_posted("evening", today_key):
                try:
                    db_mark_posted("evening", today_key)
                    text = await generate_evening_report(ALLOWED_USER_ID)
                    if text:
                        await send_html(bot, text, chat_id=ALLOWED_USER_ID)
                    logger.info(f"Evening report sent: {today_key}")
                except Exception as e:
                    logger.error(f"Evening report error: {e}")

            # Недельный обзор идей — воскресенье
            iso = now.isocalendar()
            idea_week_key = f"{iso[0]}-W{iso[1]:02d}"
            ir_h, ir_m = map(int, AUTO_IDEAREVIEW_TIME.split(":"))
            ir_mins = ir_h * 60 + ir_m
            if (now.weekday() == 6 and abs(now_mins - ir_mins) <= 1
                    and not db_was_posted("idea_review", idea_week_key)):
                try:
                    db_mark_posted("idea_review", idea_week_key)
                    text = await generate_idea_review(ALLOWED_USER_ID)
                    if text:
                        await send_html(bot, text, chat_id=ALLOWED_USER_ID)
                    logger.info(f"Idea review sent: {idea_week_key}")
                except Exception as e:
                    logger.error(f"Idea review error: {e}")

        await asyncio.sleep(45)  # 45 сек — баланс между точностью и нагрузкой на Calendar API


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
        thread_id = update.message.message_thread_id if update.message else None
        conv = conv_key(update.effective_chat.id, thread_id)
        text = await process_with_gemini(conv, prompt)
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
        f"Глеб, привет. Это Семён v{VERSION} — на связи.\n\n"
        "Пиши или говори голосом — разберу на задачи, события, идеи и ожидания, не дам забыть.\n\n"
        "Теперь я не просто планировщик, а ассистент-операционник: держу в голове цели, "
        "узкое место, деньги (раздел «Экономика») и каждый вечер думаю как стратег — "
        f"как нам выйти на {BIG_GOAL}.\n\n"
        "Команды: /focus /goals /bottleneck /projects /ideas /waiting /strategy /evening\n"
        "Напоминаю за 30 и 15 минут до встреч. Что на радаре?"
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
    thread_id = update.message.message_thread_id if update.message else None
    db_clear_history(conv_key(update.effective_chat.id, thread_id))
    await update.message.reply_text("🗑 История этой ветки очищена.")


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


async def cmd_evening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вечерняя стратегическая сводка по запросу."""
    if not is_allowed(update.effective_user.id):
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    text = await generate_evening_report(update.effective_user.id)
    await send_html(None, text, reply_to=update.message)


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args) if context.args else ""
    if arg:
        await send_reply(update, context, f"Задай фокус дня (set_goal scope=day_focus): {arg}")
    else:
        await send_reply(update, context, "Покажи фокус дня, цели недели и месяца (list_goals).")


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи все активные цели и фокус (list_goals).")


async def cmd_bottleneck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context,
        "Что сейчас сильнее всего ограничивает рост бизнеса? Проанализируй KPI и задачи, "
        "определи одно главное узкое место и зафиксируй (set_goal scope=bottleneck).")


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context,
        "Покажи проекты с рейтингом рычага (list_projects) и скажи, какой даёт наибольший рычаг сейчас.")


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи банк идей (list_ideas), сгруппируй по категориям.")


async def cmd_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, context, "Покажи, что мы ждём от других (list_waiting), отметь просроченное.")


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Стратегический разбор: как выйти на новый уровень к большой цели."""
    await send_reply(update, context,
        f"Включи режим опытного предпринимателя-стратега. Цель — {BIG_GOAL}. "
        "Вызови get_business_kpi, list_projects, list_goals. Посмотри на бизнес со стороны: "
        "где главный рычаг, что узкое место, какие 3 смелые амбициозные идеи реально выведут на новый уровень. "
        "По каждой — суть и почему это рычаг. Самые сильные сохрани через add_idea.")


async def cmd_dedup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Почистить дубли задач."""
    if not is_allowed(update.effective_user.id):
        return
    res = execute_tool("dedup_tasks", {})
    await update.message.reply_text(
        f"🧹 Почистил дубли. Локально удалено: {res.get('local_removed', 0)}, "
        f"в Google Tasks: {res.get('google_removed', 0)}."
    )


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
    app.add_handler(CommandHandler("evening",    cmd_evening))
    app.add_handler(CommandHandler("focus",      cmd_focus))
    app.add_handler(CommandHandler("goals",      cmd_goals))
    app.add_handler(CommandHandler("bottleneck", cmd_bottleneck))
    app.add_handler(CommandHandler("projects",   cmd_projects))
    app.add_handler(CommandHandler("ideas",      cmd_ideas))
    app.add_handler(CommandHandler("waiting",     cmd_waiting))
    app.add_handler(CommandHandler("strategy",   cmd_strategy))
    app.add_handler(CommandHandler("dedup",      cmd_dedup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info(f"🤖 Семён v{VERSION} запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
