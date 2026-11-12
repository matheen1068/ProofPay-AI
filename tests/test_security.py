from fastapi.testclient import TestClient
from fastapi import HTTPException
import pytest
from app.main import app, configured


def test_no_secret_means_admin_actions_fail(monkeypatch, tmp_path):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "proofpay.db"))
    client = TestClient(app)
    assert client.post("/api/checkout").status_code == 503


def test_incorrect_token_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "some-very-long-secret-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "proofpay.db"))
    client = TestClient(app)
    assert client.post("/api/checkout", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_sandbox_only(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    with pytest.raises(HTTPException) as error:
        configured()
    assert error.value.status_code == 503
