"""ProofPay Guarantee: human-approved SLA credits in PayPal's sandbox only.

Version 0.2.0 isolates each approval to its own capture, reviewed evidence
fingerprint and completed demonstration contract. This is NOT an automated
claims or production payment system.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import pathlib
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .sla import EVENTS, EXCLUSIONS, PERIOD_END, PERIOD_START, calculate

BASE = pathlib.Path(__file__).resolve().parent
SANDBOX = "https://api-m.sandbox.paypal.com"
SCENARIO_ID = "proofpay-managed-cloud-2026-09-v2"
CONTRACT_ID = "DEMO-CLOUD-SLA-2026-09"
app = FastAPI(title="ProofPay Guarantee", version="0.2.1")
lock = threading.RLock()


def db_path():
    return pathlib.Path(os.getenv("DATABASE_PATH", str(BASE.parent / "data" / "proofpay.db")))


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def evidence_snapshot():
    """Immutable representation of the active demonstration policy and data."""
    return {
        "scenario_id": SCENARIO_ID,
        "contract_id": CONTRACT_ID,
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "target_percent": "99.9",
        "credit_tiers": [
            {"below_percent": "99.9", "at_least_percent": "99", "credit_percent": 10},
            {"below_percent": "99", "at_least_percent": "95", "credit_percent": 25},
            {"below_percent": "95", "at_least_percent": "0", "credit_percent": 50},
        ],
        "fee_cents": 20000,
        "currency": "USD",
        "events": EVENTS,
        "exclusions": EXCLUSIONS,
        "evidence_provenance": "SYNTHETIC_FIXTURE_NOT_INDEPENDENTLY_VERIFIED",
    }


def evidence_hash():
    canonical = json.dumps(evidence_snapshot(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conn():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        detail TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT UNIQUE NOT NULL,
        capture_id TEXT UNIQUE,
        state TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        approved_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS investigations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        capture_id TEXT UNIQUE NOT NULL,
        paypal_refund_id TEXT, state TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        request_id TEXT UNIQUE NOT NULL)""")
    # Non-destructive migration: preserve completed v0.1 sandbox transactions.
    # Historic rows have NULL scope and can NEVER satisfy the v0.2 refund gate.
    additions = {
        "payments": {"scenario_id": "TEXT", "evidence_hash": "TEXT", "operator_role": "TEXT"},
        "investigations": {"capture_id": "TEXT", "scenario_id": "TEXT", "evidence_hash": "TEXT"},
        "refunds": {"scenario_id": "TEXT", "evidence_hash": "TEXT", "investigation_id": "INTEGER"},
    }
    for table, columns in additions.items():
        known = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for column, column_type in columns.items():
            if column not in known:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    db.execute("""CREATE TABLE IF NOT EXISTS judge_usage (
        action TEXT PRIMARY KEY, attempts INTEGER NOT NULL)""")
    db.commit()
    return db


@contextmanager
def db_session():
    db = conn()
    try:
        with db:
            yield db
    finally:
        db.close()


def record(db, kind, detail):
    db.execute("INSERT INTO events(kind,detail,created_at) VALUES(?,?,?)",
               (kind, json.dumps(detail, sort_keys=True), iso_now()))


def auth(token, action=None):
    expected = os.getenv("ADMIN_TOKEN", "")
    if len(expected) < 20:
        raise HTTPException(503, "ADMIN_TOKEN must be configured with 20+ characters")
    supplied = (token or "").removeprefix("Bearer ").strip()
    if supplied and secrets.compare_digest(supplied, expected):
        return "admin"
    judge = os.getenv("JUDGE_TOKEN", "")
    if (os.getenv("JUDGE_MODE", "false").lower() != "true"
            or len(judge) < 20 or judge == expected
            or not supplied or not secrets.compare_digest(supplied, judge)):
        raise HTTPException(401, "Invalid operator token")
    if datetime.now(timezone.utc) >= datetime(2026, 12, 16, tzinfo=timezone.utc):
        raise HTTPException(403, "Judge access has expired")
    if os.getenv("PAYPAL_BASE_URL", SANDBOX) != SANDBOX:
        raise HTTPException(503, "Judge access requires PayPal sandbox")
    limits = {"checkout": 5, "investigate": 20, "refund": 5}
    if action not in limits:
        raise HTTPException(403, "Action unavailable to judges")
    # Reserve attempts before external calls, across processes and restarts.
    # Failed attempts also consume quota so repeated failures cannot bypass limits.
    with db_session() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO judge_usage(action, attempts) VALUES(?,0)", (action,))
        changed = db.execute("UPDATE judge_usage SET attempts=attempts+1 WHERE action=? AND attempts<?",
                             (action, limits[action])).rowcount
        if not changed:
            raise HTTPException(429, "Judge testing limit reached; contact the project owner")
    return "judge"


def authorize_capture(db, capture_id, role):
    payment = scoped_payment(db, capture_id)
    if role == "judge" and payment["operator_role"] != "judge":
        raise HTTPException(403, "Judge access is limited to judge-created payments")
    return payment


def configured():
    if not os.getenv("PAYPAL_CLIENT_ID") or not os.getenv("PAYPAL_CLIENT_SECRET"):
        raise HTTPException(503, "PayPal sandbox credentials not configured")
    if os.getenv("PAYPAL_BASE_URL", SANDBOX) != SANDBOX:
        raise HTTPException(503, "Only PayPal sandbox is permitted")


def public_url():
    url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise HTTPException(503, "Invalid PUBLIC_BASE_URL")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise HTTPException(503, "Public URL must use HTTPS")
    return url


async def paypal(method, path, payload=None, request_id=None):
    configured()
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            token_response = await client.post(
                SANDBOX + "/v1/oauth2/token",
                auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                data={"grant_type": "client_credentials"})
            token_response.raise_for_status()
            token = token_response.json()["access_token"]
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            args = {"headers": headers}
            if payload is not None:
                args["json"] = payload
            response = await client.request(method, SANDBOX + path, **args)
        except httpx.RequestError as exc:
            # Never blindly retry an uncertain financial mutation.
            raise HTTPException(503, "PayPal outcome uncertain; reconcile in PayPal before retrying") from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, "PayPal OAuth authentication failed") from exc
    if response.status_code >= 400:
        try:
            body = response.json()
            details = body.get("details") or []
            reason = details[0].get("issue") if details else body.get("name", "PayPal error")
        except (ValueError, TypeError, KeyError, AttributeError):
            reason = "PayPal request failed"
        raise HTTPException(502, f"PayPal {response.status_code}: {reason}")
    try:
        return response.json() if response.content else {}
    except ValueError as exc:
        raise HTTPException(502, "Invalid PayPal response; manual reconciliation required") from exc


@app.get("/", response_class=HTMLResponse)
def page():
    return (BASE / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status():
    # Public only for this single-operator demonstration: no private customer info.
    with db_session() as db:
        payment = db.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 1").fetchone()
        capture_id = payment["capture_id"] if payment else None
        refund = db.execute("SELECT * FROM refunds WHERE capture_id=?", (capture_id,)).fetchone() if capture_id else None
        investigation = db.execute("""SELECT * FROM investigations
            WHERE capture_id=? AND scenario_id=? AND evidence_hash=? ORDER BY id DESC LIMIT 1""",
            (capture_id, SCENARIO_ID, evidence_hash())).fetchone() if capture_id else None
        timeline = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 25")]
        payment_info = dict(payment) if payment else None
        if payment_info:
            payment_info["current_scenario"] = (payment_info.get("scenario_id") == SCENARIO_ID
                and payment_info.get("evidence_hash") == evidence_hash())
        investigation_info = json.loads(investigation["result"]) if investigation else None
        if investigation_info:
            investigation_info["id"] = investigation["id"]
        return {
            "assessment": calculate(), "events": EVENTS, "exclusions": EXCLUSIONS,
            "scenario_id": SCENARIO_ID, "scenario_label": "September 2026 / SYNTHETIC 30-day completed period",
            "evidence_hash": evidence_hash(), "payment": payment_info,
            "refund": dict(refund) if refund else None,
            "investigation": investigation_info, "timeline": timeline,
            "paypal_configured": bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET")),
            "ai_configured": bool(os.getenv("OPENAI_API_KEY")),
        }


class Inquiry(BaseModel):
    capture_id: str = Field(min_length=8, max_length=100)
    additional_question: str = Field(default="", max_length=500)


class RefundApproval(BaseModel):
    capture_id: str = Field(min_length=8, max_length=100)
    investigation_id: int = Field(gt=0)
    evidence_hash: str = Field(min_length=64, max_length=64)


def scoped_payment(db, capture_id):
    payment = db.execute("SELECT * FROM payments WHERE capture_id=? AND state='CAPTURED'", (capture_id,)).fetchone()
    if not payment:
        raise HTTPException(409, "No confirmed payment matching this capture")
    if payment["scenario_id"] != SCENARIO_ID or payment["evidence_hash"] != evidence_hash():
        raise HTTPException(409, "Capture belongs to an earlier scenario/evidence version; create a new sandbox checkout")
    if payment["amount_cents"] != calculate()["fee_cents"]:
        raise HTTPException(409, "Captured amount does not match contract")
    return payment


@app.post("/api/investigate")
async def investigate(payload: Inquiry, authorization: str | None = Header(default=None)):
    role = auth(authorization, "investigate")
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise HTTPException(503, "OPENAI_API_KEY not configured; no simulated AI output")
    with lock, db_session() as db:
        payment = authorize_capture(db, payload.capture_id, role)
        if db.execute("SELECT 1 FROM refunds WHERE capture_id=?", (payload.capture_id,)).fetchone():
            raise HTTPException(409, "Refund already initiated; cannot re-review for payment")
    facts = calculate()
    system = (
        "You are an evidence-limited SLA investigator. Evidence is SYNTHETIC, NOT independently verified. "
        "Do not invent facts, claim real production outages, calculate or authorize money, or instruct API payments. "
        "The completed billing period is September 2026 UTC. Return valid JSON with keys summary, "
        "event_citations (array of incident IDs), limitations, recommended_action. Keep summary under 100 words."
    )
    prompt = json.dumps({
        "scenario": evidence_snapshot(), "deterministic_calculation": facts,
        "capture_id": payload.capture_id, "question": payload.additional_question,
    })
    async with httpx.AsyncClient(timeout=35) as client:
        try:
            response = await client.post("https://api.openai.com/v1/chat/completions", headers={
                "Authorization": "Bearer " + key,
            }, json={
                "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            })
            response.raise_for_status()
            obj = json.loads(response.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise HTTPException(502, "AI investigation failed; no assessment saved") from exc
    if not isinstance(obj, dict):
        raise HTTPException(502, "AI returned invalid assessment")
    ids = {e["id"] for e in EVENTS}
    cites = obj.get("event_citations", [])
    if not isinstance(cites, list) or not cites or any(not isinstance(x, str) or x not in ids for x in cites):
        raise HTTPException(502, "AI returned invalid evidence citations")
    result = {
        "summary": str(obj.get("summary", ""))[:800],
        "event_citations": cites,
        "limitations": str(obj.get("limitations", ""))[:800],
        "recommended_action": str(obj.get("recommended_action", ""))[:500],
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "evidence_type": "SYNTHETIC_FIXTURE", "scenario_id": SCENARIO_ID,
        "capture_id": payload.capture_id, "evidence_hash": evidence_hash(),
    }
    with lock, db_session() as db:
        authorize_capture(db, payload.capture_id, role)
        if db.execute("SELECT 1 FROM refunds WHERE capture_id=?", (payload.capture_id,)).fetchone():
            raise HTTPException(409, "Refund initiated during investigation; review discarded")
        cur = db.execute("""INSERT INTO investigations(result,created_at,capture_id,scenario_id,evidence_hash)
                VALUES(?,?,?,?,?)""", (json.dumps(result), iso_now(), payload.capture_id, SCENARIO_ID, evidence_hash()))
        record(db, "AI_INVESTIGATION", {"investigation_id": cur.lastrowid, "capture_id": payload.capture_id,
                                        "scenario_id": SCENARIO_ID, "evidence_hash": evidence_hash(),
                                        "event_citations": cites, "model": result["model"]})
        db.commit()
        result["id"] = cur.lastrowid
    return result


@app.post("/api/checkout")
async def checkout(authorization: str | None = Header(default=None)):
    role = auth(authorization, "checkout")
    base = public_url()
    order = await paypal("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": CONTRACT_ID,
            "custom_id": SCENARIO_ID,
            "description": "Demo managed cloud SLA: September 2026 synthetic evidence",
            "amount": {"currency_code": "USD", "value": "200.00"},
        }],
        "application_context": {
            "return_url": base + "/checkout/return",
            "cancel_url": base + "/?cancelled=1",
            "user_action": "PAY_NOW",
        },
    }, request_id="proofpay-order-" + str(uuid.uuid4()))
    approval = next((l["href"] for l in order.get("links", [])
                     if l.get("rel") in ("approve", "payer-action")), None)
    if not approval or not order.get("id"):
        raise HTTPException(502, "PayPal order response missing approval link")
    # Only provider-hosted PayPal URLs may be opened in the browser.
    parsed = urlsplit(approval)
    if parsed.scheme != "https" or parsed.hostname not in ("www.sandbox.paypal.com", "sandbox.paypal.com"):
        raise HTTPException(502, "Unexpected PayPal approval destination")
    with lock, db_session() as db:
        db.execute("""INSERT INTO payments(order_id,state,amount_cents,scenario_id,evidence_hash,operator_role)
                     VALUES(?,?,?,?,?,?)""", (order["id"], "CREATED", 20000, SCENARIO_ID, evidence_hash(), role))
        record(db, "ORDER_CREATED", {"order_id": order["id"], "amount": "200.00 USD",
                                     "scenario_id": SCENARIO_ID, "evidence_hash": evidence_hash()})
        db.commit()
    return {"order_id": order["id"], "approval_url": approval}


@app.get("/checkout/return")
async def checkout_return(token: str = ""):
    if not token:
        return RedirectResponse("/?payment=invalid")
    with lock, db_session() as db:
        payment = db.execute("SELECT * FROM payments WHERE order_id=?", (token,)).fetchone()
        if not payment:
            return RedirectResponse("/?payment=unknown")
        if payment["state"] == "CAPTURED":
            return RedirectResponse("/?payment=success")
        if payment["state"] != "CREATED":
            return RedirectResponse("/?payment=review")
        changed = db.execute("UPDATE payments SET state='CAPTURING' WHERE order_id=? AND state='CREATED'", (token,)).rowcount
        if not changed:
            return RedirectResponse("/?payment=review")
        db.commit()
    try:
        result = await paypal("POST", f"/v2/checkout/orders/{token}/capture", {},
                              request_id="proofpay-capture-" + token)
        if result.get("status") != "COMPLETED":
            raise ValueError("Capture not completed")
        captures = [c for pu in result.get("purchase_units", [])
                    for c in pu.get("payments", {}).get("captures", [])]
        capture = next((c for c in captures
                        if c.get("status") == "COMPLETED"
                        and c.get("amount", {}).get("currency_code") == "USD"
                        and c.get("amount", {}).get("value") == "200.00"
                        and c.get("id")), None)
        if not capture:
            raise ValueError("Confirmed capture missing or has incorrect amount")
    except Exception:
        with lock, db_session() as db:
            record(db, "CAPTURE_RECONCILIATION_REQUIRED", {"order_id": token})
            db.commit()
        return RedirectResponse("/?payment=review")
    with lock, db_session() as db:
        db.execute("UPDATE payments SET state='CAPTURED',capture_id=?,approved_at=? WHERE order_id=?",
                   (capture["id"], iso_now(), token))
        record(db, "PAYMENT_CAPTURED", {"order_id": token, "capture_id": capture["id"],
                                        "scenario_id": payment["scenario_id"]})
        db.commit()
    return RedirectResponse("/?payment=success")


@app.post("/api/refund")
async def refund(payload: RefundApproval, authorization: str | None = Header(default=None)):
    role = auth(authorization, "refund")
    assessment = calculate()
    if assessment["refund_cents"] <= 0:
        raise HTTPException(409, "No contractual refund owed")
    if payload.evidence_hash != evidence_hash():
        raise HTTPException(409, "Submitted evidence fingerprint is stale")
    with lock, db_session() as db:
        payment = authorize_capture(db, payload.capture_id, role)
        review = db.execute("""SELECT * FROM investigations WHERE id=? AND capture_id=?
            AND scenario_id=? AND evidence_hash=?""",
            (payload.investigation_id, payload.capture_id, SCENARIO_ID, evidence_hash())).fetchone()
        if not review:
            raise HTTPException(409, "No AI investigation tied to this capture and evidence version")
        if assessment["refund_cents"] > payment["amount_cents"]:
            raise HTTPException(409, "Credit exceeds captured amount")
        if db.execute("SELECT 1 FROM refunds WHERE capture_id=?", (payload.capture_id,)).fetchone():
            raise HTTPException(409, "Existing refund: do not duplicate; inspect provider state")

    # Re-read PayPal capture before any money movement. The provider, not the model,
    # confirms that this exact USD 200 payment was actually captured.
    confirmed = await paypal("GET", f"/v2/payments/captures/{payload.capture_id}")
    if (confirmed.get("id") != payload.capture_id
            or confirmed.get("status") != "COMPLETED"
            or confirmed.get("amount", {}).get("currency_code") != "USD"
            or confirmed.get("amount", {}).get("value") != "200.00"):
        raise HTTPException(409, "PayPal capture status/amount mismatch; reconciliation required")
    with lock, db_session() as db:
        # Repeat all preconditions after the network await to avoid TOCTOU reuse.
        authorize_capture(db, payload.capture_id, role)
        if not db.execute("""SELECT 1 FROM investigations WHERE id=? AND capture_id=?
            AND scenario_id=? AND evidence_hash=?""",
            (payload.investigation_id, payload.capture_id, SCENARIO_ID, evidence_hash())).fetchone():
            raise HTTPException(409, "Investigation is no longer valid")
        if db.execute("SELECT 1 FROM refunds WHERE capture_id=?", (payload.capture_id,)).fetchone():
            raise HTTPException(409, "Existing refund; inspect provider state")
        request_id = "proofpay-refund-" + str(uuid.uuid4())
        try:
            db.execute("""INSERT INTO refunds(capture_id,state,amount_cents,request_id,scenario_id,evidence_hash,investigation_id)
                VALUES(?,?,?,?,?,?,?)""", (payload.capture_id, "PENDING", assessment["refund_cents"],
                                              request_id, SCENARIO_ID, evidence_hash(), payload.investigation_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Refund already initiated") from exc
        record(db, "MERCHANT_REFUND_APPROVED", {
            "capture_id": payload.capture_id, "investigation_id": payload.investigation_id,
            "evidence_hash": evidence_hash(), "amount_cents": assessment["refund_cents"],
            "provider_capture_reverified": True,
        })
        db.commit()

    try:
        response = await paypal("POST", f"/v2/payments/captures/{payload.capture_id}/refund", {
            "amount": {"currency_code": "USD", "value": f"{Decimal(assessment['refund_cents']) / 100:.2f}"},
            "note_to_payer": "Synthetic demo SLA contractual service credit",
        }, request_id=request_id)
    except Exception:
        with lock, db_session() as db:
            db.execute("UPDATE refunds SET state='RECONCILE' WHERE capture_id=?", (payload.capture_id,))
            record(db, "REFUND_RECONCILIATION_REQUIRED", {"capture_id": payload.capture_id})
            db.commit()
        raise HTTPException(503, "Refund outcome uncertain; manually reconcile with PayPal; request locked")
    if response.get("status") not in ("COMPLETED", "PENDING") or not response.get("id"):
        with lock, db_session() as db:
            db.execute("UPDATE refunds SET state='RECONCILE' WHERE capture_id=?", (payload.capture_id,))
            db.commit()
        raise HTTPException(503, "Unexpected refund status; reconcile before retry")
    with lock, db_session() as db:
        db.execute("UPDATE refunds SET state=?,paypal_refund_id=? WHERE capture_id=?",
                   (response["status"], response["id"], payload.capture_id))
        record(db, "PAYPAL_REFUND_RESULT", {"capture_id": payload.capture_id,
                                            "investigation_id": payload.investigation_id,
                                            "refund_id": response["id"], "status": response["status"]})
        db.commit()
    return {"paypal_refund_id": response["id"], "status": response["status"],
            "capture_id": payload.capture_id, "amount_cents": assessment["refund_cents"]}
