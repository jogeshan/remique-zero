import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db_session
from app.models import (
    Reminder,
    Task,
    TelegramAccount,
    Transaction,
    User,
    UserSetting,
)
from app.services import (
    api_token_service,
    finance_service,
    memory_service,
    reminder_service,
    task_service,
)

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1", tags=["dashboard-api"])
page_router = APIRouter(tags=["dashboard"])

ADMIN_ROLES = ("admin", "super_admin")
MAX_TRANSACTION_LIMIT = 200
RECENT_COMPLETED_TASKS = 20
MEMORY_LIMIT = 100


# --- helpers ------------------------------------------------------------


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _amount(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


async def _get_user_setting(
    session: AsyncSession,
    user_id,
) -> UserSetting | None:
    return await session.scalar(
        select(UserSetting).where(UserSetting.user_id == user_id)
    )


async def _user_timezone_name(
    session: AsyncSession,
    user_id,
) -> str:
    setting = await _get_user_setting(session, user_id)
    if setting is not None and setting.timezone:
        return setting.timezone
    return settings.default_timezone


# --- authentication -------------------------------------------------------


async def get_current_api_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """
    Authenticate an `Authorization: Bearer <token>` header.

    The token alone identifies the user; there is deliberately no
    way for a caller to select a different user.
    """
    header = request.headers.get("authorization", "")
    scheme, _, raw_token = header.partition(" ")

    if scheme.lower() != "bearer" or not raw_token.strip():
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await api_token_service.authenticate_token(
        session,
        raw_token.strip(),
    )

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_admin(
    user: User = Depends(get_current_api_user),
) -> User:
    if user.role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Administrator access is required.",
        )
    return user


# --- endpoints ------------------------------------------------------------


@api_router.get("/me")
async def get_me(
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.scalar(
        select(TelegramAccount).where(
            TelegramAccount.user_id == user.id
        )
    )
    setting = await _get_user_setting(session, user.id)

    return {
        "telegram_id": account.telegram_user_id if account else None,
        "username": account.username if account else None,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "timezone": (
            setting.timezone if setting else settings.default_timezone
        ),
        "language": setting.language if setting else "en",
        "member_since": _iso(user.created_at),
    }


@api_router.get("/summary")
async def get_summary(
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    tz = _zone(await _user_timezone_name(session, user.id))
    now = datetime.now(timezone.utc)

    month_since, month_until = finance_service.period_range(
        "month", tz, now
    )
    month = await finance_service.summarize(
        session, user.id, since=month_since, until=month_until
    )

    y_since, y_until = finance_service.period_range(
        "yesterday", tz, now
    )
    yesterday = await finance_service.summarize(
        session, user.id, since=y_since, until=y_until
    )

    totals = await finance_service.debt_totals(session, user.id)

    return {
        "currency": finance_service.DEFAULT_CURRENCY,
        "month": month,
        "yesterday": yesterday,
        "debts": totals,
    }


@api_router.get("/transactions")
async def get_transactions(
    limit: int = Query(default=50, ge=1, le=MAX_TRANSACTION_LIMIT),
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    now = datetime.now(timezone.utc)
    rows = await finance_service.list_transactions(
        session,
        user.id,
        since=now - timedelta(days=366),
        until=now + timedelta(days=1),
        limit=limit,
    )

    return {
        "transactions": [
            {
                "id": str(row.id),
                "type": row.type,
                "amount": _amount(row.amount),
                "currency": row.currency,
                "category": row.category,
                "note": row.note,
                "occurred_at": _iso(row.occurred_at),
            }
            for row in rows
        ]
    }


def _debt_json(debt) -> dict:
    return {
        "id": str(debt.id),
        "direction": debt.direction,
        "kind": debt.kind,
        "counterparty": debt.counterparty,
        "principal": _amount(debt.principal),
        "remaining": _amount(debt.remaining),
        "status": debt.status,
        "note": debt.note,
        "due_date": _iso(debt.due_date),
        "created_at": _iso(debt.created_at),
    }


@api_router.get("/debts")
async def get_debts(
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    open_debts = await finance_service.list_debts(
        session, user.id, status="open"
    )
    settled_debts = await finance_service.list_debts(
        session, user.id, status="settled"
    )
    totals = await finance_service.debt_totals(session, user.id)

    return {
        "open": [_debt_json(debt) for debt in open_debts],
        "settled": [_debt_json(debt) for debt in settled_debts],
        "totals": totals,
    }


@api_router.get("/reminders")
async def get_reminders(
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    pairs = await reminder_service.list_active_reminders(
        session, user.id
    )

    return {
        "reminders": [
            {
                "id": str(reminder.id),
                "title": reminder.title,
                "description": reminder.description,
                "timezone": reminder.timezone,
                "recurrence": reminder_service.recurrence_label(
                    reminder.recurrence_rule
                ),
                "next_at": _iso(next_at),
            }
            for reminder, next_at in pairs
        ]
    }


def _task_json(task) -> dict:
    return {
        "id": str(task.id),
        "title": task.title,
        "notes": task.notes,
        "status": task.status,
        "due_at": _iso(task.due_at),
        "completed_at": _iso(task.completed_at),
        "created_at": _iso(task.created_at),
    }


@api_router.get("/tasks")
async def get_tasks(
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    open_tasks = await task_service.list_tasks(
        session, user.id, status="open"
    )
    done_tasks = await task_service.list_tasks(
        session, user.id, status="done"
    )
    done_tasks.sort(
        key=lambda task: task.completed_at or task.updated_at,
        reverse=True,
    )

    return {
        "open": [_task_json(task) for task in open_tasks],
        "recently_completed": [
            _task_json(task)
            for task in done_tasks[:RECENT_COMPLETED_TASKS]
        ],
    }


@api_router.get("/memories")
async def get_memories(
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    memories = await memory_service.list_memories(
        session, user.id, limit=MEMORY_LIMIT
    )

    return {
        "memories": [
            {
                "id": str(memory.id),
                "content": memory.content,
                "category": memory.category,
                "created_at": _iso(memory.created_at),
            }
            for memory in memories
        ]
    }


@api_router.get("/admin/stats")
async def get_admin_stats(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Platform-wide counts for administrators (no personal content)."""
    tz = _zone(settings.default_timezone)
    month_since, month_until = finance_service.period_range(
        "month", tz, datetime.now(timezone.utc)
    )

    users = await session.scalar(
        select(func.count()).select_from(User)
    )
    active_reminders = await session.scalar(
        select(func.count())
        .select_from(Reminder)
        .where(Reminder.status == "active")
    )
    transactions_this_month = await session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.occurred_at >= month_since,
            Transaction.occurred_at < month_until,
        )
    )
    open_tasks = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.status == "open")
    )

    return {
        "users": users or 0,
        "active_reminders": active_reminders or 0,
        "transactions_this_month": transactions_this_month or 0,
        "open_tasks": open_tasks or 0,
    }


# --- dashboard page ---------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remique Zero — Dashboard</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #0f1420;
         color: #e8ecf3; }
  main { max-width: 860px; margin: 0 auto; padding: 20px 16px 48px; }
  h1 { font-size: 22px; } h2 { font-size: 17px; margin-top: 28px; }
  form { display: flex; gap: 8px; flex-wrap: wrap; }
  input[type=password] { flex: 1; min-width: 240px; padding: 10px;
    border-radius: 8px; border: 1px solid #334; background: #1a2233;
    color: #e8ecf3; }
  button { padding: 10px 18px; border: 0; border-radius: 8px;
    background: #3b82f6; color: #fff; cursor: pointer; }
  .card { background: #1a2233; border-radius: 10px; padding: 14px 16px;
    margin-top: 10px; }
  .grid { display: flex; gap: 12px; flex-wrap: wrap; }
  .stat { flex: 1; min-width: 140px; }
  .stat .num { font-size: 22px; font-weight: 600; }
  .stat .lbl { color: #9fb0c9; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; }
  td, th { padding: 6px 4px; border-bottom: 1px solid #26314a;
    text-align: left; font-size: 14px; }
  ul { margin: 6px 0; padding-left: 20px; }
  li { margin: 4px 0; font-size: 14px; }
  .muted { color: #9fb0c9; font-size: 13px; }
  .err { color: #f87171; }
  .pos { color: #4ade80; } .neg { color: #f87171; }
</style>
</head>
<body>
<main>
  <h1>🤖 Remique Zero — Dashboard</h1>
  <p class="muted">Paste the API token from the bot's /apitoken command.
  It is kept only in this tab's session storage.</p>
  <form id="token-form">
    <input type="password" id="token" placeholder="rz_..." autocomplete="off">
    <button type="submit">Load my data</button>
  </form>
  <p id="status"></p>

  <section id="overview" hidden>
    <h2>Overview — this month</h2>
    <div class="grid">
      <div class="card stat"><div class="num pos" id="ov-income"></div>
        <div class="lbl">Income</div></div>
      <div class="card stat"><div class="num neg" id="ov-expense"></div>
        <div class="lbl">Expenses</div></div>
      <div class="card stat"><div class="num" id="ov-balance"></div>
        <div class="lbl">Balance</div></div>
      <div class="card stat"><div class="num" id="ov-yesterday"></div>
        <div class="lbl">Spent yesterday</div></div>
    </div>
  </section>

  <section id="transactions" hidden>
    <h2>Transactions</h2>
    <div class="card"><table><thead><tr><th>When</th><th>Type</th>
      <th>Category</th><th>Note</th><th>Amount</th></tr></thead>
      <tbody id="tx-body"></tbody></table></div>
  </section>

  <section id="debts" hidden>
    <h2>Debts &amp; loans</h2>
    <div class="card"><ul id="debt-list"></ul>
      <p class="muted" id="debt-totals"></p></div>
  </section>

  <section id="reminders" hidden>
    <h2>Reminders</h2>
    <div class="card"><ul id="reminder-list"></ul></div>
  </section>

  <section id="tasks" hidden>
    <h2>Tasks</h2>
    <div class="card"><ul id="task-list"></ul></div>
  </section>

  <section id="memories" hidden>
    <h2>Memories</h2>
    <div class="card"><ul id="memory-list"></ul></div>
  </section>
</main>
<script>
(function () {
  var tokenInput = document.getElementById('token');
  var status = document.getElementById('status');
  var token = sessionStorage.getItem('rz_api_token') || '';

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function money(value, currency) {
    var n = Number(value || 0);
    return n.toLocaleString('en-US', {maximumFractionDigits: 2}) +
      ' ' + (currency || '');
  }

  function when(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    return isNaN(d) ? esc(iso) : d.toLocaleString();
  }

  function api(path) {
    return fetch(path, {headers: {'Authorization': 'Bearer ' + token}})
      .then(function (r) {
        if (!r.ok) throw new Error('Request failed (' + r.status + ')');
        return r.json();
      });
  }

  function show(id) { document.getElementById(id).hidden = false; }

  function render(summary, txs, debts, reminders, tasks, memories) {
    var cur = summary.currency || '';
    document.getElementById('ov-income').textContent =
      money(summary.month.total_income, cur);
    document.getElementById('ov-expense').textContent =
      money(summary.month.total_expense, cur);
    document.getElementById('ov-balance').textContent =
      money(summary.month.balance, cur);
    document.getElementById('ov-yesterday').textContent =
      money(summary.yesterday.total_expense, cur);
    show('overview');

    var rows = txs.transactions.map(function (t) {
      return '<tr><td>' + when(t.occurred_at) + '</td><td>' +
        esc(t.type) + '</td><td>' + esc(t.category) + '</td><td>' +
        esc(t.note || '') + '</td><td>' + money(t.amount, t.currency) +
        '</td></tr>';
    });
    document.getElementById('tx-body').innerHTML =
      rows.length ? rows.join('') :
        '<tr><td colspan="5" class="muted">No transactions yet.</td></tr>';
    show('transactions');

    var debtItems = debts.open.map(function (d) {
      var verb = d.direction === 'borrowed' ? 'You owe' : 'Owes you';
      return '<li>' + verb + ' <b>' + esc(d.counterparty) + '</b> — ' +
        money(d.remaining, cur) + ' remaining of ' +
        money(d.principal, cur) + '</li>';
    });
    document.getElementById('debt-list').innerHTML =
      debtItems.length ? debtItems.join('') :
        '<li class="muted">No open debts.</li>';
    document.getElementById('debt-totals').textContent =
      'You owe ' + money(debts.totals.owed_by_user, cur) +
      ' · owed to you ' + money(debts.totals.owed_to_user, cur);
    show('debts');

    var remItems = reminders.reminders.map(function (r) {
      var extra = r.recurrence ? ' · ' + esc(r.recurrence) : '';
      return '<li><b>' + esc(r.title) + '</b> — next: ' +
        when(r.next_at) + extra + '</li>';
    });
    document.getElementById('reminder-list').innerHTML =
      remItems.length ? remItems.join('') :
        '<li class="muted">No active reminders.</li>';
    show('reminders');

    var taskItems = tasks.open.map(function (t) {
      var due = t.due_at ? ' (due ' + when(t.due_at) + ')' : '';
      return '<li>' + esc(t.title) + due + '</li>';
    });
    document.getElementById('task-list').innerHTML =
      taskItems.length ? taskItems.join('') :
        '<li class="muted">No open tasks.</li>';
    show('tasks');

    var memItems = memories.memories.map(function (m) {
      return '<li>' + esc(m.content) +
        ' <span class="muted">(' + esc(m.category) + ')</span></li>';
    });
    document.getElementById('memory-list').innerHTML =
      memItems.length ? memItems.join('') :
        '<li class="muted">No memories saved.</li>';
    show('memories');
  }

  function load() {
    if (!token) { status.textContent = 'Enter your API token first.'; return; }
    status.textContent = 'Loading…';
    status.className = '';
    Promise.all([
      api('/api/v1/summary'),
      api('/api/v1/transactions?limit=50'),
      api('/api/v1/debts'),
      api('/api/v1/reminders'),
      api('/api/v1/tasks'),
      api('/api/v1/memories')
    ]).then(function (results) {
      status.textContent = '';
      render(results[0], results[1], results[2], results[3],
             results[4], results[5]);
    }).catch(function (err) {
      status.textContent = err.message +
        ' — check the token (get a fresh one with /apitoken).';
      status.className = 'err';
    });
  }

  document.getElementById('token-form').addEventListener('submit',
    function (event) {
      event.preventDefault();
      token = tokenInput.value.trim();
      sessionStorage.setItem('rz_api_token', token);
      load();
    });

  if (token) { tokenInput.value = token; load(); }
})();
</script>
</body>
</html>
"""


@page_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return HTMLResponse(content=DASHBOARD_HTML)
