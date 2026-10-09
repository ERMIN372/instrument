import pytest

from app.deploy_config import validate_deployment
from app import mail_import


def test_relaxdev_requires_password_and_database(monkeypatch):
    monkeypatch.setenv("RELAXDEV_DEPLOY", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_deployment()
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/example")
    with pytest.raises(RuntimeError, match="APP_PASSWORD"):
        validate_deployment()
    monkeypatch.setenv("APP_PASSWORD", "test-secret")
    validate_deployment()


def test_relaxdev_does_not_start_mail_automatically(monkeypatch):
    monkeypatch.setenv("RELAXDEV_DEPLOY", "1")
    monkeypatch.setenv("MAIL_HOST", "imap.example.org")
    monkeypatch.delenv("MAIL_IMPORT_ENABLED", raising=False)
    assert mail_import.start() is None
    assert mail_import.snapshot()["enabled"] is False


def test_local_mode_unchanged(monkeypatch):
    monkeypatch.delenv("RELAXDEV_DEPLOY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    validate_deployment()
