from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
import sqlite3
import os
import math
import httpx
import json

app = FastAPI(title="Churn Whisper", description="SaaS churn prediction using behavioral signals")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.getenv("DB_PATH", "churn.db")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            mrr REAL DEFAULT 0,
            signup_date DATE,
            last_login DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            properties TEXT,
            occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS churn_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            score REAL NOT NULL,
            risk_level TEXT NOT NULL,
            signals TEXT,
            ai_recommendation TEXT,
            calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS churn_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );
    """)
    conn.commit()
    conn.close()


init_db()


class AccountCreate(BaseModel):
    id: str
    name: str
    email: str
    plan: Optional[str] = "free"
    mrr: Optional[float] = 0
    signup_date: Optional[str] = None


class EventCreate(BaseModel):
    event_type: str
    properties: Optional[Dict[str, Any]] = {}
    occurred_at: Optional[str] = None


class BulkEvents(BaseModel):
    events: List[EventCreate]


def calculate_churn_score(account_id: str, conn) -> dict:
    """
    Score 0-100: higher = more likely to churn.
    Uses weighted signals from behavioral data.
    """
    account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not account:
        return None

    signals = []
    score = 0.0
    today = date.today()

    # Signal 1: Days since last login (weight: 30)
    if account["last_login"]:
        last_login = date.fromisoformat(account["last_login"])
        days_inactive = (today - last_login).days
        if days_inactive > 30:
            s = min(30, days_inactive / 2)
            score += s
            signals.append({"signal": "inactivity", "detail": f"{days_inactive} days since last login", "weight": round(s, 1)})
        elif days_inactive > 14:
            score += 15
            signals.append({"signal": "reduced_activity", "detail": f"{days_inactive} days since last login", "weight": 15})

    # Signal 2: Event frequency decline (weight: 25)
    now = datetime.utcnow().isoformat()
    last_30 = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE account_id = ? AND occurred_at >= datetime('now', '-30 days')",
        (account_id,)
    ).fetchone()["cnt"]
    prev_30 = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE account_id = ? AND occurred_at BETWEEN datetime('now', '-60 days') AND datetime('now', '-30 days')",
        (account_id,)
    ).fetchone()["cnt"]

    if prev_30 > 0:
        decline_pct = (prev_30 - last_30) / prev_30
        if decline_pct > 0.5:
            s = min(25, decline_pct * 30)
            score += s
            signals.append({"signal": "usage_decline", "detail": f"{round(decline_pct*100)}% drop in activity (last 30d vs prior 30d)", "weight": round(s, 1)})
    elif last_30 == 0 and prev_30 == 0:
        score += 20
        signals.append({"signal": "no_activity", "detail": "No events in 60 days", "weight": 20})

    # Signal 3: Support tickets / errors (weight: 20)
    error_events = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE account_id = ? AND event_type IN ('error','support_ticket','complaint') AND occurred_at >= datetime('now', '-30 days')",
        (account_id,)
    ).fetchone()["cnt"]
    if error_events >= 3:
        s = min(20, error_events * 4)
        score += s
        signals.append({"signal": "friction", "detail": f"{error_events} errors/support tickets in 30 days", "weight": round(s, 1)})

    # Signal 4: Core feature disengagement (weight: 20)
    core_events = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE account_id = ? AND event_type LIKE 'core_%' AND occurred_at >= datetime('now', '-30 days')",
        (account_id,)
    ).fetchone()["cnt"]
    if core_events == 0:
        score += 20
        signals.append({"signal": "core_feature_dropout", "detail": "No core feature usage in 30 days", "weight": 20})
    elif core_events < 3:
        score += 10
        signals.append({"signal": "low_core_usage", "detail": f"Only {core_events} core feature events in 30 days", "weight": 10})

    # Signal 5: Account age vs. engagement (weight: 5)
    if account["signup_date"]:
        signup = date.fromisoformat(account["signup_date"])
        age_days = (today - signup).days
        if age_days < 14 and last_30 < 5:
            score += 5
            signals.append({"signal": "poor_onboarding", "detail": "New account with low engagement", "weight": 5})

    score = min(100, round(score, 1))

    if score >= 70:
        risk = "critical"
    elif score >= 45:
        risk = "high"
    elif score >= 25:
        risk = "medium"
    else:
        risk = "low"

    return {
        "account_id": account_id,
        "score": score,
        "risk_level": risk,
        "signals": signals,
        "account": dict(account),
    }


async def get_ai_recommendation(account: dict, signals: list) -> str:
    if not ANTHROPIC_API_KEY:
        return "Set ANTHROPIC_API_KEY to get AI-powered retention recommendations."

    signal_text = "\n".join([f"- {s['signal']}: {s['detail']} (weight: {s['weight']})" for s in signals])
    prompt = f"""You are a SaaS customer success expert. An account is at risk of churning.

Account info:
- Name: {account['name']}
- Plan: {account['plan']}
- MRR: ${account['mrr']}
- Churn score: {account.get('score', 'N/A')}/100

Risk signals detected:
{signal_text}

Write a concise, actionable retention playbook (3-5 bullet points) that a CSM should execute THIS WEEK to save this account. Be specific about outreach messages and actions."""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 512, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        return resp.json()["content"][0]["text"]


async def send_slack_alert(account: dict, score: float, risk: str, recommendation: str):
    if not SLACK_WEBHOOK_URL:
        return
    color = {"critical": "#ff0000", "high": "#ff8800", "medium": "#ffcc00", "low": "#00cc00"}[risk]
    payload = {
        "attachments": [{
            "color": color,
            "title": f"Churn Alert: {account['name']}",
            "fields": [
                {"title": "Churn Score", "value": f"{score}/100", "short": True},
                {"title": "Risk Level", "value": risk.upper(), "short": True},
                {"title": "Plan", "value": account["plan"], "short": True},
                {"title": "MRR", "value": f"${account['mrr']}", "short": True},
                {"title": "Recommendation", "value": recommendation[:300]},
            ],
        }]
    }
    async with httpx.AsyncClient() as client:
        await client.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)


@app.post("/accounts", status_code=201)
def create_account(account: AccountCreate):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO accounts (id, name, email, plan, mrr, signup_date) VALUES (?,?,?,?,?,?)",
            (account.id, account.name, account.email, account.plan, account.mrr,
             account.signup_date or date.today().isoformat()),
        )
        conn.commit()
        return {"id": account.id, "name": account.name}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Account ID already exists")
    finally:
        conn.close()


@app.put("/accounts/{account_id}")
def update_account(account_id: str, data: dict):
    conn = get_db()
    acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not acc:
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")
    allowed = ["name", "email", "plan", "mrr", "last_login"]
    updates = [(k, v) for k, v in data.items() if k in allowed]
    if updates:
        set_clause = ", ".join(f"{k}=?" for k, _ in updates)
        conn.execute(f"UPDATE accounts SET {set_clause} WHERE id=?", [v for _, v in updates] + [account_id])
        conn.commit()
    conn.close()
    return {"message": "Updated"}


@app.post("/accounts/{account_id}/events")
def track_event(account_id: str, event: EventCreate):
    conn = get_db()
    acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not acc:
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")
    occurred = event.occurred_at or datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO events (account_id, event_type, properties, occurred_at) VALUES (?,?,?,?)",
        (account_id, event.event_type, json.dumps(event.properties), occurred),
    )
    conn.execute("UPDATE accounts SET last_login=? WHERE id=?", (occurred[:10], account_id))
    conn.commit()
    conn.close()
    return {"message": "Event tracked"}


@app.post("/accounts/{account_id}/events/bulk")
def track_bulk_events(account_id: str, payload: BulkEvents):
    conn = get_db()
    acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not acc:
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")
    for event in payload.events:
        occurred = event.occurred_at or datetime.utcnow().isoformat()
        conn.execute(
            "INSERT INTO events (account_id, event_type, properties, occurred_at) VALUES (?,?,?,?)",
            (account_id, event.event_type, json.dumps(event.properties), occurred),
        )
    conn.commit()
    conn.close()
    return {"message": f"{len(payload.events)} events tracked"}


@app.get("/accounts/{account_id}/score")
async def get_churn_score(account_id: str, background_tasks: BackgroundTasks):
    conn = get_db()
    result = calculate_churn_score(account_id, conn)
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Account not found")

    recommendation = await get_ai_recommendation({**result["account"], "score": result["score"]}, result["signals"])
    result["ai_recommendation"] = recommendation

    conn = get_db()
    conn.execute(
        "INSERT INTO churn_scores (account_id, score, risk_level, signals, ai_recommendation) VALUES (?,?,?,?,?)",
        (account_id, result["score"], result["risk_level"], json.dumps(result["signals"]), recommendation),
    )
    conn.commit()
    conn.close()

    if result["risk_level"] in ("critical", "high"):
        background_tasks.add_task(send_slack_alert, result["account"], result["score"], result["risk_level"], recommendation)

    return result


@app.get("/at-risk")
def get_at_risk_accounts(min_score: Optional[float] = 45):
    conn = get_db()
    rows = conn.execute("""
        SELECT cs.account_id, cs.score, cs.risk_level, cs.signals, cs.ai_recommendation, cs.calculated_at,
               a.name, a.email, a.plan, a.mrr
        FROM churn_scores cs
        JOIN accounts a ON cs.account_id = a.id
        WHERE cs.id IN (
            SELECT MAX(id) FROM churn_scores GROUP BY account_id
        )
        AND cs.score >= ?
        ORDER BY cs.score DESC
    """, (min_score,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["signals"] = json.loads(d["signals"] or "[]")
        result.append(d)
    return {"at_risk_count": len(result), "accounts": result}


@app.post("/score-all")
async def score_all_accounts():
    conn = get_db()
    accounts = conn.execute("SELECT id FROM accounts").fetchall()
    conn.close()
    results = []
    for acc in accounts:
        conn = get_db()
        result = calculate_churn_score(acc["id"], conn)
        conn.close()
        if result:
            conn = get_db()
            conn.execute(
                "INSERT INTO churn_scores (account_id, score, risk_level, signals) VALUES (?,?,?,?)",
                (acc["id"], result["score"], result["risk_level"], json.dumps(result["signals"])),
            )
            conn.commit()
            conn.close()
            results.append({"id": acc["id"], "score": result["score"], "risk": result["risk_level"]})
    return {"scored": len(results), "results": results}


@app.get("/health")
def health():
    return {"status": "ok", "service": "churn-whisper"}
