import concurrent.futures
import pytest
from fastapi import HTTPException
import app.main as appmod

ADMIN = 'test-admin-token-long-enough'
JUDGE = 'test-judge-token-long-enough'

@pytest.fixture
def judge(monkeypatch, tmp_path):
    monkeypatch.setenv('ADMIN_TOKEN', ADMIN)
    monkeypatch.setenv('JUDGE_TOKEN', JUDGE)
    monkeypatch.setenv('JUDGE_MODE', 'true')
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'judge.db'))
    monkeypatch.setenv('PAYPAL_BASE_URL', appmod.SANDBOX)
    with appmod.db_session():
        pass
    return JUDGE


def test_judge_disabled_by_default(judge, monkeypatch):
    monkeypatch.delenv('JUDGE_MODE')
    with pytest.raises(HTTPException) as error:
        appmod.auth(judge, 'checkout')
    assert error.value.status_code == 401


def test_judge_cannot_use_admin_payment(judge):
    with appmod.db_session() as db:
        db.execute('INSERT INTO payments(order_id,capture_id,state,amount_cents,scenario_id,evidence_hash) VALUES(?,?,?,?,?,?)',
                   ('ORDER-TEST', 'CAPTURE-TEST', 'CAPTURED', 20000, appmod.SCENARIO_ID, appmod.evidence_hash()))
    with appmod.db_session() as db:
        with pytest.raises(HTTPException) as error:
            appmod.authorize_capture(db, 'CAPTURE-TEST', 'judge')
        assert error.value.status_code == 403
        assert appmod.authorize_capture(db, 'CAPTURE-TEST', 'admin')['amount_cents'] == 20000


def test_judge_quota_is_atomic_and_persistent(judge):
    def attempt(_):
        try:
            return appmod.auth(judge, 'checkout')
        except HTTPException as error:
            return error.status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(12)))
    assert results.count('judge') == 5
    assert results.count(429) == 7
    with appmod.db_session() as db:
        assert db.execute("SELECT attempts FROM judge_usage WHERE action='checkout'").fetchone()[0] == 5
    assert appmod.auth(ADMIN, 'checkout') == 'admin'


def test_judge_rejects_live_paypal(judge, monkeypatch):
    monkeypatch.setenv('PAYPAL_BASE_URL', 'https://api-m.paypal.com')
    with pytest.raises(HTTPException) as error:
        appmod.auth(judge, 'checkout')
    assert error.value.status_code == 503
