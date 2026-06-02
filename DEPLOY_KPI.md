# Финансовый мост: Семён ↔ task-board (раздел «Экономика»)

Семён теперь умеет читать живые бизнес-метрики из финансовой системы через
read-only эндпоинт `GET /api/kpi` в проекте **task-board**.

## Что изменилось в коде

**task-board / server.js**
- Добавлен эндпоинт `GET /api/kpi` (перед `app.listen`).
- Авторизация по заголовку `Authorization: Bearer <SEMYON_TOKEN>` — отдельно от парольной cookie.
- Переиспользует существующую `buildFinanceContext()` — те же цифры, что видит Лариса.

**alta-assistant / main.py**
- `fetch_business_kpi()` — тянет KPI по HTTP, кэш 5 минут (stdlib `urllib`, новых зависимостей нет).
- Инструмент Gemini `get_business_kpi` — Семён сам вызывает при вопросах про деньги/продажи/кассу.
- Блок «БИЗНЕС-МЕТРИКИ ALTA» в системном промпте — цифры всегда перед глазами.

## Переменные окружения (Railway)

### Проект task-board → Variables
```
SEMYON_TOKEN = Np7Rii6tZAPRiRHMYY-gKsB5Y4goj6GQoqIh3IIzDfU
```
(можно заменить на свой; главное — чтобы совпадал с FINANCE_API_TOKEN ниже)

### Проект alta-assistant → Variables
```
FINANCE_API_URL   = https://alta-production-82f7.up.railway.app/api/kpi
FINANCE_API_TOKEN = Np7Rii6tZAPRiRHMYY-gKsB5Y4goj6GQoqIh3IIzDfU
```

## Шаги деплоя

1. Закоммить и запушить **task-board** (Railway задеплоит сам).
2. Добавить `SEMYON_TOKEN` в Variables проекта task-board.
3. Проверить эндпоинт:
   ```
   curl -H "Authorization: Bearer <SEMYON_TOKEN>" \
        https://alta-production-82f7.up.railway.app/api/kpi
   ```
   Должен вернуть JSON с цифрами (а без токена — 401).
4. Закоммить и запушить **alta-assistant**.
5. Добавить `FINANCE_API_URL` и `FINANCE_API_TOKEN` в Variables проекта alta-assistant.
6. В Телеграме спросить Семёна: «сколько денег пришло за месяц?» / «какая дебиторка?» —
   он вызовет get_business_kpi и ответит реальными числами.

## Контракт ответа /api/kpi
```json
{
  "as_of": "2026-06-02",
  "received_this_month": 0,
  "received_this_year": 2675787,
  "contracts_this_month": 0,
  "receivables_total": 1477000,
  "receivables_overdue": 290000,
  "presale_pipeline": 0,
  "presale_leads": 0,
  "kp_outstanding": 0,
  "meetings_scheduled": 0,
  "expenses_this_month": 0,
  "top_overdue": [{ "client": "НКО", "amount": 100000, "dueDate": "2026-02-16" }]
}
```

## Безопасность
- Токен даёт доступ только на ЧТЕНИЕ агрегированных KPI. Нельзя менять финансы.
- Парольный вход в `/finance/` не затронут.
- Если `SEMYON_TOKEN` не задан в task-board — эндпоинт всегда отвечает 401 (fail-safe).
