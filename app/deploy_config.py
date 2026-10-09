"""Deployment-only safeguards. Do not affect existing VM or local tests."""
import os


def validate_deployment() -> None:
    if os.environ.get("RELAXDEV_DEPLOY") != "1":
        return
    if not os.environ.get("DATABASE_URL", "").strip():
        raise RuntimeError("RelaxDev: DATABASE_URL is required (create a separate PostgreSQL)")
    if not os.environ.get("APP_PASSWORD", "").strip():
        raise RuntimeError("RelaxDev: APP_PASSWORD is required (HTTP Basic Auth cannot be disabled)")
    if not os.environ.get("APP_USER", "admin").strip():
        raise RuntimeError("RelaxDev: APP_USER must not be empty")
    if os.environ.get("MAIL_HOST") and os.environ.get("MAIL_IMPORT_ENABLED") != "1":
        # Email importer is deliberately not started before old/new data reconciliation.
        pass
