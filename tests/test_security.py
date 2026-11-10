from fastapi.testclient import TestClient
from app.main import app

def test_no_secret_means_admin_actions_fail(monkeypatch,tmp_path):
    monkeypatch.delenv('ADMIN_TOKEN',raising=False)
    client=TestClient(app)
    assert client.post('/api/refund').status_code==503

def test_incorrect_token_rejected(monkeypatch):
    monkeypatch.setenv('ADMIN_TOKEN','some-very-long-secret-token')
    client=TestClient(app)
    assert client.post('/api/checkout',headers={'Authorization':'Bearer wrong'}).status_code==401

def test_sandbox_only(monkeypatch):
    from app.main import configured
    from fastapi import HTTPException
    import pytest
    monkeypatch.setenv('PAYPAL_CLIENT_ID','test')
    monkeypatch.setenv('PAYPAL_CLIENT_SECRET','test')
    monkeypatch.setenv('PAYPAL_BASE_URL','https://api-m.paypal.com')
    with pytest.raises(HTTPException) as e: configured()
    assert e.value.status_code==503
