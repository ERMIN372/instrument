import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
-- Источник = один тип выгрузки (колонка в сводной таблице).
CREATE TABLE IF NOT EXISTS sources (
    id       serial PRIMARY KEY,
    name     text NOT NULL UNIQUE,
    -- sum: сумма за период; last: значение на последний день периода (остатки)
    agg      text NOT NULL DEFAULT 'sum' CHECK (agg IN ('sum', 'last')),
    position integer NOT NULL DEFAULT 0,
    hidden   boolean NOT NULL DEFAULT false
);

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


def init() -> None:
    pool.open(wait=True, timeout=60)
    with pool.connection() as conn:
        conn.execute(SCHEMA)
