"""Разбор писем для автозагрузки с почты (без IMAP и БД)."""
from email.message import EmailMessage

import pytest

from app import mail_import


def _mail(sender: str, files: dict[str, bytes], forwarded: bool = False) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = "Выгрузка"
    msg.set_content("см. вложение")
    for name, data in files.items():
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    if forwarded:
        outer = EmailMessage()
        outer["From"] = sender
        outer.set_content("пересылаю")
        outer.add_attachment(msg)
        msg = outer
    return msg.as_bytes()


def _collect():
    got = []

    def ingest(name, data):
        got.append((name, data))
        return {"file": name, "ok": True, "source": "x", "date_from": "", "date_to": "", "rows": 0}
    return got, ingest


def test_takes_xlsx_with_cyrillic_names_from_allowed_sender():
    got, ingest = _collect()
    raw = _mail("1С <robot@firma.ru>", {"Выпуск_сентябрь_2026.xlsx": b"x1", "readme.txt": b"t", "Остатки.XLSX": b"x2"})
    res = mail_import.process_message(raw, ["robot@firma.ru"], ingest)
    assert got == [("Выпуск_сентябрь_2026.xlsx", b"x1"), ("Остатки.XLSX", b"x2")]
    assert len(res) == 2


def test_domain_allow_and_forwarded_message():
    got, ingest = _collect()
    raw = _mail("Иван <Ivan@Firma.ru>", {"Заказы.xlsx": b"z"}, forwarded=True)
    mail_import.process_message(raw, ["@firma.ru"], ingest)
    assert got == [("Заказы.xlsx", b"z")]


def test_stranger_is_ignored():
    got, ingest = _collect()
    raw = _mail("evil@firma.ru.evil.com", {"Выпуск.xlsx": b"x"})
    assert mail_import.process_message(raw, ["@firma.ru", "robot@firma.ru"], ingest) == []
    assert got == []


def test_bad_file_is_reported_not_raised(monkeypatch):
    res = mail_import.ingest_file("Выпуск.xlsx", b"not a zip")
    assert res["ok"] is False and res["source"] == "Выпуск"


def test_config_requires_sender_list(monkeypatch):
    monkeypatch.delenv("MAIL_HOST", raising=False)
    assert mail_import.config() is None
    monkeypatch.setenv("MAIL_HOST", "imap.yandex.ru")
    monkeypatch.setenv("MAIL_FROM", " ")
    with pytest.raises(ValueError):
        mail_import.config()
    monkeypatch.setenv("MAIL_FROM", "Robot@Firma.ru, @firma.ru")
    assert mail_import.config()["allowed"] == ["robot@firma.ru", "@firma.ru"]


class FakeImap:
    """Ящик из двух писем: UID 7 — непрочитанное, его и ждём в FETCH/STORE."""

    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def login(self, user, password):
        self.calls.append(("login", user))

    def select(self, folder):
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b"7"]
        if cmd == "FETCH":
            return "OK", [(b"7 (UID 7 BODY[] {1}", self.raw), b")"]
        return "OK", [None]


def test_poll_reads_unseen_without_marking_then_marks_seen(monkeypatch):
    got, ingest = _collect()
    imap = FakeImap(_mail("robot@firma.ru", {"Выпуск.xlsx": b"x"}))
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL", lambda *a, **k: imap)
    cfg = {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX", "allowed": ["robot@firma.ru"]}
    assert len(mail_import.poll_once(cfg, ingest)) == 1
    assert ("SEARCH", None, "UNSEEN") in imap.calls
    assert ("FETCH", b"7", "(BODY.PEEK[])") in imap.calls  # PEEK: сам FETCH не помечает прочитанным
    assert imap.calls[-1] == ("STORE", b"7", "+FLAGS", "(\\Seen)")


def test_db_failure_leaves_message_unseen(monkeypatch):
    def broken(name, data):
        raise OSError("БД недоступна")
    imap = FakeImap(_mail("robot@firma.ru", {"Выпуск.xlsx": b"x"}))
    monkeypatch.setattr(mail_import.imaplib, "IMAP4_SSL", lambda *a, **k: imap)
    cfg = {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX", "allowed": ["robot@firma.ru"]}
    with pytest.raises(OSError):
        mail_import.poll_once(cfg, broken)
    assert not any(c[0] == "STORE" for c in imap.calls)
