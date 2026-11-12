"""No internet calls; verifies real API call orchestration with mocked providers."""
import json
import sqlite3
from fastapi.testclient import TestClient
import pytest
import app.main as appmod

TOKEN = "local-test-operator-token-long-enough"
HEADERS = {"Authorization": "Bearer " + TOKEN}
CAPTURE_A = "CAPTURE-TEST-00001"
CAPTURE_B = "CAPTURE-TEST-00002"

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "database.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "local-placeholder-not-a-real-secret")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "mock")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "mock")
    monkeypatch.delenv("PAYPAL_BASE_URL", raising=False)
    return TestClient(appmod.app)


def payment(db, order_id="ORDER-1", capture_id=CAPTURE_A, legacy=False):
    db.execute("""INSERT INTO payments(order_id,capture_id,state,amount_cents,scenario_id,evidence_hash)
        VALUES(?,?,?,?,?,?)""", (order_id, capture_id, "CAPTURED", 20000,
                              None if legacy else appmod.SCENARIO_ID,
                              None if legacy else appmod.evidence_hash()))
    db.commit()


def review(db, capture_id=CAPTURE_A, legacy=False):
    result = {"summary": "Fictional incident", "event_citations": ["INC-101"],
              "limitations": "Synthetic data", "recommended_action": "Review", "model": "mock"}
    cur = db.execute("""INSERT INTO investigations(result,created_at,capture_id,scenario_id,evidence_hash)
        VALUES(?,?,?,?,?)""", (json.dumps(result), appmod.iso_now(), capture_id,
                                  None if legacy else appmod.SCENARIO_ID,
                                  None if legacy else appmod.evidence_hash()))
    db.commit()
    return cur.lastrowid


def payload(capture_id, review_id):
    return {"capture_id": capture_id, "investigation_id": review_id,
            "evidence_hash": appmod.evidence_hash()}


def test_scenario_is_completed_and_synthetic(client):
    info = client.get("/api/status").json()
    assert info["scenario_label"].startswith("September 2026")
    assert info["assessment"]["period_end"] < "2026-11-12"
    assert info["assessment"]["eligible_downtime_minutes"] == 120
    assert info["assessment"]["refund_cents"] == 2000
    assert len(info["evidence_hash"]) == 64
    assert info["assessment"]["evidence_type"] == "SYNTHETIC_FIXTURE"


def test_historic_records_migrate_but_cannot_fund_new_review(client):
    # Simulate exact old v0.1 schema with no capture/evidence scoping.
    with sqlite3.connect(appmod.db_path()) as old:
        old.execute("""CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,capture_id TEXT UNIQUE,state TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,approved_at TEXT)""")
        old.execute("INSERT INTO payments(order_id,capture_id,state,amount_cents) VALUES(?,?,?,?)",
                    ("HISTORICAL-ORDER", CAPTURE_A, "CAPTURED", 20000))
    state = client.get("/api/status").json()
    assert state["payment"]["order_id"] == "HISTORICAL-ORDER"
    assert state["payment"]["current_scenario"] is False
    assert client.post("/api/investigate", json={"capture_id": CAPTURE_A}, headers=HEADERS).status_code == 409


def test_cross_capture_investigation_is_rejected(client, monkeypatch):
    with appmod.db_session() as db:
        payment(db, "ORDER-A", CAPTURE_A)
        payment(db, "ORDER-B", CAPTURE_B)
        review_id = review(db, CAPTURE_A)
    async def never_called(*args, **kwargs):
        raise AssertionError("PayPal must not be called on mismatched review")
    monkeypatch.setattr(appmod, "paypal", never_called)
    r = client.post("/api/refund", json=payload(CAPTURE_B, review_id), headers=HEADERS)
    assert r.status_code == 409
    assert "investigation" in r.json()["detail"].lower()


def test_stale_evidence_hash_rejected(client, monkeypatch):
    with appmod.db_session() as db:
        payment(db)
        review_id = review(db)
    r = client.post("/api/refund", json={**payload(CAPTURE_A, review_id), "evidence_hash": "0" * 64}, headers=HEADERS)
    assert r.status_code == 409


def test_paypal_capture_mismatch_blocks_refund(client, monkeypatch):
    with appmod.db_session() as db:
        payment(db)
        review_id = review(db)
    async def fake_paypal(method, path, *args, **kwargs):
        assert method == "GET"
        return {"id": CAPTURE_A, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "180.00"}}
    monkeypatch.setattr(appmod, "paypal", fake_paypal)
    response = client.post("/api/refund", json=payload(CAPTURE_A, review_id), headers=HEADERS)
    assert response.status_code == 409
    with appmod.db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 0


def test_exact_capture_refund_and_duplicate_prevention(client, monkeypatch):
    with appmod.db_session() as db:
        payment(db)
        review_id = review(db)
    calls = []
    async def fake_paypal(method, path, payload=None, request_id=None):
        calls.append((method, path, payload, request_id))
        if method == "GET":
            return {"id": CAPTURE_A, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "200.00"}}
        return {"id": "SANDBOX-REFUND-TEST", "status": "COMPLETED"}
    monkeypatch.setattr(appmod, "paypal", fake_paypal)
    res = client.post("/api/refund", json=payload(CAPTURE_A, review_id), headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["amount_cents"] == 2000
    assert len(calls) == 2
    assert calls[0][0] == "GET" and CAPTURE_A in calls[0][1]
    assert calls[1][0] == "POST" and calls[1][2]["amount"]["value"] == "20.00"
    assert calls[1][3].startswith("proofpay-refund-")
    duplicate = client.post("/api/refund", json=payload(CAPTURE_A, review_id), headers=HEADERS)
    assert duplicate.status_code == 409
    assert len(calls) == 2  # absolutely no second PayPal request
    state = client.get("/api/status").json()
    assert state["refund"]["state"] == "COMPLETED"
    assert state["refund"]["investigation_id"] == review_id


def test_refund_network_uncertainty_locks_retry(client, monkeypatch):
    with appmod.db_session() as db:
        payment(db)
        review_id = review(db)
    calls = []
    async def fake_paypal(method, path, payload=None, request_id=None):
        calls.append(method)
        if method == "GET":
            return {"id": CAPTURE_A, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "200.00"}}
        raise RuntimeError("simulate timeout after provider received request")
    monkeypatch.setattr(appmod, "paypal", fake_paypal)
    r = client.post("/api/refund", json=payload(CAPTURE_A, review_id), headers=HEADERS)
    assert r.status_code == 503
    assert client.get("/api/status").json()["refund"]["state"] == "RECONCILE"
    assert client.post("/api/refund", json=payload(CAPTURE_A, review_id), headers=HEADERS).status_code == 409
    assert calls == ["GET", "POST"]


def test_checkout_and_capture_record_scope(client, monkeypatch):
    async def fake_paypal(method, path, payload=None, request_id=None):
        if path == "/v2/checkout/orders":
            assert payload["purchase_units"][0]["custom_id"] == appmod.SCENARIO_ID
            return {"id": "ORDER-TEST-123", "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-TEST-123"}]}
        assert path == "/v2/checkout/orders/ORDER-TEST-123/capture"
        return {"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": CAPTURE_A, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "200.00"}}]}}]}
    monkeypatch.setattr(appmod, "paypal", fake_paypal)
    creation = client.post("/api/checkout", headers=HEADERS)
    assert creation.status_code == 200, creation.text
    redirect = client.get("/checkout/return?token=ORDER-TEST-123", follow_redirects=False)
    assert redirect.status_code in (302, 307)
    assert redirect.headers["location"] == "/?payment=success"
    state = client.get("/api/status").json()
    assert state["payment"]["state"] == "CAPTURED"
    assert state["payment"]["current_scenario"] is True


def test_invalid_ai_citations_rejected(client, monkeypatch):
    with appmod.db_session() as db:
        payment(db)
    class FakeResult:
        def raise_for_status(self): return None
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "summary": "Sample", "event_citations": ["NOT-EXISTENT"], "limitations": "Synthetic", "recommended_action": "Review"})}}]}
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return FakeResult()
    monkeypatch.setattr(appmod.httpx, "AsyncClient", FakeAsyncClient)
    result = client.post("/api/investigate", json={"capture_id": CAPTURE_A}, headers=HEADERS)
    assert result.status_code == 502
    with appmod.db_session() as db:
        assert db.execute("SELECT count(*) FROM investigations").fetchone()[0] == 0
