import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
-- Источник = один тип выгрузки (колонка в сводной таблице).
CREATE TABLE IF NOT EXISTS sources (
    id       serial PRIMARY KEY,
    name     text NOT NULL UNIQUE,
    -- sum: сумма за период; last: остаток — срез на начало периода и на начало следующего
    agg      text NOT NULL DEFAULT 'sum' CHECK (agg IN ('sum', 'last')),
    position integer NOT NULL DEFAULT 0,
    hidden   boolean NOT NULL DEFAULT false
);
-- Заголовок колонки «остаток на конец» (для agg = last); NULL — выводится из name.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS close_name text;
-- Прежние имена: файлы со старым именем источника попадают в переименованную колонку.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS aliases text[] NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS items (
    code      text PRIMARY KEY,
    uid       text,
    name      text NOT NULL,
    base_unit text NOT NULL DEFAULT 'шт',
    pack_size integer,
    category  text NOT NULL DEFAULT 'Прочее'
);

CREATE TABLE IF NOT EXISTS uploads (
    id          bigserial PRIMARY KEY,
    source_id   integer NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    filename    text NOT NULL,
    date_from   date NOT NULL,
    date_to     date NOT NULL,
    rows        integer NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS movements (
    source_id integer NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    upload_id bigint  NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    day       date    NOT NULL,
    item_code text    NOT NULL REFERENCES items(code),
    qty       double precision NOT NULL,  -- в единицах файла (шт / упак / кг)
    qty_base  double precision NOT NULL,  -- в базовых единицах (шт / кг)
    PRIMARY KEY (source_id, day, item_code)
);

CREATE INDEX IF NOT EXISTS movements_day_idx ON movements (day);
CREATE INDEX IF NOT EXISTS movements_upload_idx ON movements (upload_id);
"""

pool = ConnectionPool(
    os.environ.get("DATABASE_URL", "postgresql://instrument:instrument@localhost:5432/instrument"),
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)


# Разовое переименование колонок сводной (сентябрь 2026) в нужном порядке.
RENAMES = [
    ("ОстаткиПоДням", "Остатки на начало периода"),
    ("ПланированиеПроизводства", "Заказ склада"),
    ("ПланПроизводстваПоДням", "План производства"),
    ("ФактПроизводстваПоДням", "Выпуск производства"),
    ("ЗаказыПоДням", "Заказ покупателей"),
]


def rename_sources(conn) -> None:
    """Срабатывает, только пока есть источники со старыми именами, поэтому ручные
    переименования и порядок из вкладки «Источники» потом не перетирает."""
    with conn.transaction():
        rows = conn.execute("SELECT id, name FROM sources ORDER BY position, id").fetchall()
        names = {r["name"] for r in rows}
        done = False
        for old, new in RENAMES:
            # регистр сравниваем в Python: lower() в PostgreSQL при локали C не знает кириллицу
            r = next((r for r in rows if r["name"].casefold() == old.casefold()), None)
            if r and new not in names:
                conn.execute("UPDATE sources SET name = %s, aliases = array_append(aliases, name) WHERE id = %s",
                             (new, r["id"]))
                names.add(new)
                r["name"] = new
                done = True
        if not done:
            return
        order = [new for _, new in RENAMES]
        # sorted стабилен: остальные источники сохраняют свой порядок после переименованных.
        rows.sort(key=lambda r: order.index(r["name"]) if r["name"] in order else len(order))
        for pos, r in enumerate(rows, 1):
            conn.execute("UPDATE sources SET position = %s WHERE id = %s", (pos, r["id"]))


def init() -> None:
    pool.open(wait=True, timeout=60)
    with pool.connection() as conn:
        conn.execute(SCHEMA)
        rename_sources(conn)
