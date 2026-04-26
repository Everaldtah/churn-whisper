# Churn Whisper

**SaaS churn prediction engine — detect at-risk customers before they cancel using behavioral signals.**

## Problem It Solves

SaaS companies lose 5–10% of revenue monthly to churn, yet most only learn about it when the cancellation email arrives. Churn Whisper continuously analyzes user behavior signals — login frequency, feature usage, error rates, support tickets — and scores every account with a 0–100 churn risk score, then uses AI to generate a personalized retention playbook for each at-risk account.

## Features

- **Behavioral event tracking** — instrument your app to send login, feature usage, error, and support events
- **Churn risk scoring** — proprietary 5-signal model scores every account 0–100
- **Risk classification** — Low / Medium / High / Critical tiers
- **AI retention playbooks** — Claude generates specific, actionable CSM plays per at-risk account
- **Slack alerts** — real-time notifications when an account crosses critical threshold
- **At-risk dashboard** — filterable list of all accounts above a score threshold
- **Bulk scoring** — score all accounts in one API call (run nightly via cron)
- **Score history** — track how accounts trend over time

## Churn Signals Detected

| Signal | Weight | Description |
|--------|--------|-------------|
| Inactivity | 30 pts | Days since last login |
| Usage decline | 25 pts | Drop in event frequency vs. prior period |
| Friction events | 20 pts | Errors, support tickets, complaints |
| Core feature dropout | 20 pts | No core feature usage in 30 days |
| Poor onboarding | 5 pts | New account with low early engagement |

## Tech Stack

- **Backend**: Python 3.11+ / FastAPI
- **AI**: Anthropic Claude (claude-haiku-4-5-20251001)
- **Database**: SQLite (swap to PostgreSQL for production)
- **Alerts**: Slack webhooks

## Installation

```bash
git clone https://github.com/Everaldtah/churn-whisper
cd churn-whisper
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
uvicorn main:app --reload
```

API docs at `http://localhost:8000/docs`

## Integration Guide

### Step 1: Register your customer accounts
```bash
curl -X POST http://localhost:8000/accounts \
  -H "Content-Type: application/json" \
  -d '{"id":"cust_123","name":"Acme Corp","email":"admin@acme.com","plan":"pro","mrr":299}'
```

### Step 2: Send behavioral events from your app
```bash
# User logged in
curl -X POST http://localhost:8000/accounts/cust_123/events \
  -H "Content-Type: application/json" \
  -d '{"event_type":"login"}'

# Core feature used
curl -X POST http://localhost:8000/accounts/cust_123/events \
  -H "Content-Type: application/json" \
  -d '{"event_type":"core_export","properties":{"format":"csv"}}'

# Error occurred
curl -X POST http://localhost:8000/accounts/cust_123/events \
  -H "Content-Type: application/json" \
  -d '{"event_type":"error","properties":{"code":500,"page":"dashboard"}}'
```

### Step 3: Get churn score + AI recommendation
```bash
curl http://localhost:8000/accounts/cust_123/score
```

### Step 4: See all at-risk accounts
```bash
curl http://localhost:8000/at-risk?min_score=45
```

### Step 5: Score all accounts (run nightly)
```bash
curl -X POST http://localhost:8000/score-all
```

## Sample Output

```json
{
  "account_id": "cust_123",
  "score": 72.5,
  "risk_level": "critical",
  "signals": [
    {"signal": "inactivity", "detail": "18 days since last login", "weight": 15.0},
    {"signal": "usage_decline", "detail": "65% drop in activity", "weight": 19.5},
    {"signal": "core_feature_dropout", "detail": "No core feature usage in 30 days", "weight": 20.0}
  ],
  "ai_recommendation": "• Schedule an executive business review this week..."
}
```

## Monetization Model

| Plan | Price | Accounts |
|------|-------|----------|
| Starter | $49/mo | Up to 500 accounts |
| Growth | $149/mo | Up to 2,500 accounts + Slack alerts |
| Scale | $399/mo | Unlimited accounts + CRM sync + custom signals |
| Enterprise | Custom | Dedicated scoring model, on-prem option |

**Ideal buyers**: B2B SaaS companies with $10K–$500K MRR who have a CS team but no churn prediction tooling.
