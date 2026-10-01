"""Автозагрузка с почты: xlsx из писем забираются по IMAP и грузятся так же,
как через сайт (источник — по имени файла, повтор за период заменяет данные).

Включается, если в окружении задан MAIL_HOST:
    MAIL_HOST      imap.yandex.ru / imap.gmail.com / imap.mail.ru …
    MAIL_PORT      993 (IMAP по SSL)
    MAIL_USER      ящик, куда приходят выгрузки
    MAIL_PASSWORD  пароль приложения (обычный пароль почтовики по IMAP не пускают)
    MAIL_FROM      от кого принимать, через запятую: адреса или домены (@firma.ru) — обязательно
    MAIL_FOLDER    папка, по умолчанию INBOX
    MAIL_INTERVAL  как часто проверять, секунд (по умолчанию 600)

Берутся только непрочитанные письма, после обработки письмо помечается прочитанным.
Поэтому ящик нужен отдельный: письмо, открытое человеком раньше робота, пропустится.
Если упала БД — письмо остаётся непрочитанным и обработается при следующей проверке.

Проверить настройки разово:  python -m app.mail_import
"""
from __future__ import annotations

import datetime as dt
import email
import imaplib
import logging
import os
import threading
from collections import deque
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses

from . import db, service
from .parser import parse_xlsx, source_from_filename

MAX_FILE_MB = 50

log = logging.getLogger("instrument.mail")
if not log.handlers:  # uvicorn настраивает только свои логгеры, INFO иначе не видно
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     mail: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

# Для /api/mail и блока «Почта» на вкладке «Загрузка»: когда проверяли, чем кончилось
# и последние события (файл загружен / ошибка файла / письмо пропущено). Живёт до
# перезапуска сервиса; загруженные файлы остаются и в истории загрузок (via = mail).
status: dict = {"enabled": False}
events: deque = deque(maxlen=30)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _event(**kw) -> None:
    events.appendleft({"at": _now(), **kw})


def snapshot() -> dict:
    return {**status, "events": list(events)}


def config() -> dict | None:
    host = os.environ.get("MAIL_HOST", "").strip()
    if not host:
        return None
    allowed = [a.strip().lower() for a in os.environ.get("MAIL_FROM", "").split(",") if a.strip()]
    if not allowed:
        # Без списка любой, кто узнает адрес ящика, подменит данные в сводной.
        raise ValueError("MAIL_FROM пуст: укажи, от кого принимать выгрузки")
    return {
        "host": host,
        "port": int(os.environ.get("MAIL_PORT") or 993),
        "user": os.environ.get("MAIL_USER", ""),
        "password": os.environ.get("MAIL_PASSWORD", ""),
        "folder": os.environ.get("MAIL_FOLDER") or "INBOX",
        "allowed": allowed,
        "interval": max(60, int(os.environ.get("MAIL_INTERVAL") or 600)),
    }


def sender_allowed(msg: EmailMessage, allowed: list[str]) -> bool:
    for _, addr in getaddresses(msg.get_all("From", [])):
        addr = addr.lower()
        if any(addr == a or (a.startswith("@") and addr.endswith(a)) for a in allowed):
            return True
    return False


def xlsx_attachments(msg: EmailMessage) -> list[tuple[str, bytes]]:
    """Вложения .xlsx, в том числе из пересланных писем."""
    out = []
    for part in msg.walk():
        name = part.get_filename()
        if name and name.lower().endswith(".xlsx"):
            out.append((name, part.get_payload(decode=True) or b""))
    return out


def ingest_file(name: str, data: bytes) -> dict:
    """Ошибки файла (битый, не те колонки) — в результат: повтор не поможет.
    Ошибки БД пробрасываются, чтобы письмо осталось непрочитанным."""
    source = source_from_filename(name)
    try:
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise ValueError(f"файл больше {MAX_FILE_MB} МБ")
        parsed = parse_xlsx(data)
    except Exception as e:  # noqa: BLE001
        return {"file": name, "ok": False, "source": source, "error": str(e)}
    with db.pool.connection() as conn:
        return {"file": name, "ok": True, **service.ingest(conn, name, source, parsed, via="mail")}


def process_message(raw: bytes, allowed: list[str], ingest=ingest_file) -> list[dict]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    who, subject = msg.get("From", ""), msg.get("Subject", "")
    if not sender_allowed(msg, allowed):
        log.warning("пропускаю письмо от %s: отправителя нет в MAIL_FROM", who)
        _event(kind="skip", sender=who, subject=subject, reason="отправителя нет в MAIL_FROM")
        return []
    files = xlsx_attachments(msg)
    if not files:
        log.info("в письме от %s «%s» нет xlsx", who, subject)
        _event(kind="skip", sender=who, subject=subject, reason="нет вложений .xlsx")
    results = [ingest(name, data) for name, data in files]
    for r in results:
        if r["ok"]:
            log.info("%s → %s, %s–%s, строк %s", r["file"], r["source"], r["date_from"], r["date_to"], r["rows"])
        else:
            log.error("%s: %s", r["file"], r["error"])
        _event(kind="file", subject=subject, **{k: r.get(k) for k in
               ("file", "ok", "source", "date_from", "date_to", "rows", "error")})
    return results


def poll_once(cfg: dict, ingest=ingest_file) -> list[dict]:
    results = []
    with imaplib.IMAP4_SSL(cfg["host"], cfg["port"], timeout=60) as imap:
        imap.login(cfg["user"], cfg["password"])
        typ, _ = imap.select(cfg["folder"])
        if typ != "OK":
            raise RuntimeError(f"нет папки {cfg['folder']}")
        _, data = imap.uid("SEARCH", None, "UNSEEN")
        for uid in data[0].split():  # UID растут со временем: свежий файл грузится последним
            _, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next(p[1] for p in fetched if isinstance(p, tuple))
            results += process_message(raw, cfg["allowed"], ingest)
            imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    return results


def _loop(cfg: dict, stop: threading.Event) -> None:
    while not stop.is_set():
        status["checked_at"] = _now()
        try:
            if poll_once(cfg):
                status["loaded_at"] = status["checked_at"]
            status["error"] = None
        except Exception as e:  # noqa: BLE001 — сеть или БД: повторим в следующий раз
            status["error"] = f"{type(e).__name__}: {e}"
            log.error("проверка почты не удалась: %s", status["error"])
        stop.wait(cfg["interval"])


def start() -> threading.Event | None:
    """Фоновая проверка ящика; вернёт событие для остановки или None, если почта не настроена."""
    try:
        cfg = config()
    except ValueError as e:
        status.update(enabled=False, error=str(e))
        log.error("%s — автозагрузка с почты выключена", e)
        return None
    if not cfg:
        return None
    status.update(enabled=True, mailbox=cfg["user"], interval=cfg["interval"])
    log.info("проверяю %s каждые %s с", cfg["user"], cfg["interval"])
    stop = threading.Event()
    threading.Thread(target=_loop, args=(cfg, stop), name="mail-import", daemon=True).start()
    return stop


if __name__ == "__main__":
    cfg = config()
    if not cfg:
        raise SystemExit("MAIL_HOST не задан — почта не настроена")
    db.init()
    res = poll_once(cfg)
    print(f"обработано файлов: {len(res)}, с ошибкой: {sum(not r['ok'] for r in res)}")
