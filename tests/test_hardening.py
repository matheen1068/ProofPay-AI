from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.sla import EVENTS, EXCLUSIONS, PERIOD_END, parse_utc

TOKEN = 'local-test-only-operator-token'
HEADERS = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'proofpay.db'))
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.setenv('PROOFPAY_MODE', 'merchant')
    monkeypatch.setenv('ADMIN_TOKEN', TOKEN)
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    monkeypatch.setenv('PAYPAL_CLIENT_ID', 'test')
    monkeypatch.setenv('PAYPAL_CLIENT_SECRET', 'test')
    monkeypatch.setenv('PAYPAL_BASE_URL', main.SANDBOX)
    return TestClient(main.app)


def test_fixture_is_a_completed_period():
    now = datetime.now(timezone.utc)
    assert all(parse_utc(x['end']) <= parse_utc(PERIOD_END) < now for x in EVENTS + EXCLUSIONS)


def test_judge_isolated_even_with_merchant_credentials(client, monkeypatch):
    monkeypatch.setenv('PROOFPAY_MODE', 'judge')
    def unexpected():
        pytest.fail('Judge mode must not open the merchant database')
    monkeypatch.setattr(main, 'conn', unexpected)
    provider = AsyncMock(side_effect=AssertionError('Unexpected provider call'))
    monkeypatch.setattr(main, 'paypal', provider)
    with client:
        review = client.post('/api/judge/review').json()
        assert review['assessment']['refund_cents'] == 2000
        assert review['payment_provider_called'] is False
        assert client.get('/api/status', headers=HEADERS).json()['payment'] is None
        for path, payload in (
            ('checkout', {}), ('investigate', {'capture_id': 'a'}),
            ('refund', dict(capture_id='a', review_id=1, evidence_digest='digest', amount_cents=2000)),
        ):
            assert client.post('/api/' + path, headers=HEADERS, json=payload).status_code == 403
        assert client.get('/checkout/return?token=order-a').status_code == 403
    provider.assert_not_called()


def test_capture_review_and_refund_binding(client, monkeypatch):
    with closing(main.conn()) as db:
        db.executemany("INSERT INTO payments(order_id,capture_id,state,amount_cents) VALUES(?,?,'CAPTURED',20000)",
                       [('order-a', 'capture-a'), ('order-b', 'capture-b')])
        legacy = db.execute("INSERT INTO investigations(result,created_at) VALUES('{}','old')").lastrowid
        db.commit()
    assert client.get('/api/status').json()['payment'] is None
    assert client.get('/api/status', headers=HEADERS).json()['payment']['capture_id'] == 'capture-b'
    assert client.post('/api/judge/review').status_code == 404

    response = httpx.Response(200, request=httpx.Request('POST', 'https://api.openai.com'),
        json={'choices': [{'message': {'content': '{"summary":"Synthetic fixture","event_citations":["INC-101"]}'}}]})
    class ModelClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return SimpleNamespace(post=AsyncMock(return_value=response))
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(main.httpx, 'AsyncClient', ModelClient)
    result = client.post('/api/investigate', headers=HEADERS, json={'capture_id':'capture-a'})
    assert result.status_code == 200
    review = result.json()
    approval = {key: review[key] for key in ('capture_id', 'review_id', 'evidence_digest')}
    approval['amount_cents'] = 2000
    provider = AsyncMock(return_value={'id': 'refund-a', 'status':'COMPLETED'})
    monkeypatch.setattr(main, 'paypal', provider)
    def approve(**changes):
        return client.post('/api/refund', headers=HEADERS, json=approval | changes)
    assert approve(capture_id='capture-b').status_code == 409
    assert approve(evidence_digest='stale').status_code == 409
    assert approve(review_id=legacy).status_code == 409
    assert approve(amount_cents=1999).status_code == 409
    with monkeypatch.context() as changed:
        changed.setitem(EVENTS[0], 'detail', 'Revised evidence')
        assert approve().status_code == 409
    provider.assert_not_called()
    assert approve().status_code == 200
    assert provider.await_args.args[1] == '/v2/payments/captures/capture-a/refund'
    assert provider.await_args.args[2]['amount']['value'] == '20.00'
    with TestClient(main.app) as reopened:
        assert reopened.post('/api/refund', headers=HEADERS, json=approval).status_code == 409
    assert provider.await_count == 1


def test_render_refuses_ephemeral_merchant_db(client, monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    with pytest.raises(RuntimeError, match='persistent disk'):
        with client:
            pass


def test_render_requires_https_origin(client, monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.setattr(main, 'DB_PATH', '/var/data/proofpay.db')
    monkeypatch.setattr(main.pathlib.Path, 'is_mount', lambda self: True)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://localhost:8000')
    with pytest.raises(RuntimeError, match='HTTPS PUBLIC_BASE_URL'):
        with client:
            pass
