import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id          bigserial PRIMARY KEY,
    filename    text NOT NULL,
    source      text NOT NULL,
    sheets      text[] NOT NULL DEFAULT '{}',
    date_from   date,
    date_to     date,
    facts       integer NOT NULL DEFAULT 0,
    uploaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS metrics (
    id       bigserial PRIMARY KEY,
    source   text NOT NULL,
    name     text NOT NULL,
    label    text,
    agg      text NOT NULL DEFAULT 'sum' CHECK (agg IN ('sum', 'avg', 'last')),
    hidden   boolean NOT NULL DEFAULT false,
    position integer NOT NULL DEFAULT 0,
    UNIQUE (source, name)
);

CREATE TABLE IF NOT EXISTS facts (
    metric_id bigint NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    upload_id bigint NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    day       date   NOT NULL,
    dims      jsonb  NOT NULL DEFAULT '{}',
    value     double precision NOT NULL,
    n         integer NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS facts_day_metric_idx ON facts (day, metric_id);
CREATE INDEX IF NOT EXISTS facts_upload_idx ON facts (upload_id);
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
