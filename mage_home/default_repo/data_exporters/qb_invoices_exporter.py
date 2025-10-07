import json, time
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import execute_values
from mage_ai.data_preparation.shared.secrets import get_secret_value
if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE IF NOT EXISTS raw.qb_invoices (
  id  text PRIMARY KEY,
  payload jsonb NOT NULL,
  ingested_at_utc timestamptz NOT NULL DEFAULT now(),
  extract_window_start_utc timestamptz NOT NULL,
  extract_window_end_utc   timestamptz NOT NULL,
  page_number int,
  page_size   int,
  request_payload jsonb
);
CREATE INDEX IF NOT EXISTS idx_qb_invoices_ingested_at ON raw.qb_invoices (ingested_at_utc);
CREATE INDEX IF NOT EXISTS idx_qb_invoices_win_start   ON raw.qb_invoices (extract_window_start_utc);
CREATE INDEX IF NOT EXISTS idx_qb_invoices_win_end     ON raw.qb_invoices (extract_window_end_utc);
"""

UPSERT = """
INSERT INTO raw.qb_invoices (
  id, payload, extract_window_start_utc, extract_window_end_utc,
  page_number, page_size, request_payload
)
VALUES %s
ON CONFLICT (id) DO UPDATE
SET payload = EXCLUDED.payload,
    ingested_at_utc = now(),
    extract_window_start_utc = EXCLUDED.extract_window_start_utc,
    extract_window_end_utc   = EXCLUDED.extract_window_end_utc,
    page_number = EXCLUDED.page_number,
    page_size   = EXCLUDED.page_size,
    request_payload = EXCLUDED.request_payload
WHERE raw.qb_invoices.payload IS DISTINCT FROM EXCLUDED.payload
RETURNING
  (xmax = 0)  AS inserted,
  (xmax <> 0) AS updated;
"""
#XMAX es una función propia de postgresql que nos ayuda a  verificar si una fila fue insertada o actualizada
def _conn():
    return psycopg2.connect(
        host=get_secret_value('DB_HOST') or 'postgres',
        port=int(get_secret_value('DB_PORT') or 5432),
        dbname=get_secret_value('DB_NAME') or 'qbo_dw',
        user=get_secret_value('DB_USER'),
        password=get_secret_value('DB_PASSWORD'),
    )

def _utc_iso_now():
    return datetime.now(timezone.utc).isoformat()

def _iterate_batches(lst, batch_size=1000):
    for i in range(0, len(lst), batch_size):
        yield lst[i:i+batch_size]

@data_exporter
def export_invoices_to_postgres(data, *args, **kwargs):
    if not isinstance(data, dict):
        raise ValueError("Se esperaba un dict como output del loader.")
    rows = data.get('data') or []
    audit = data.get('audit') or []
    if not rows:
        print("No hay invoices para insertar.")
        return {"processed": 0}

    # Deduplicación simple por Id
    seen = {}
    for r in rows:
        if isinstance(r, dict) and r.get("Id"):
            seen[r["Id"]] = r
    rows = list(seen.values())
    if not rows:
        print("Sin filas válidas con Id para invoices.")
        return {"processed": 0}

    win = audit[0] if audit else {
        "window_start_utc": "2001-01-01T00:00:00Z",
        "window_end_utc": "2025-09-11T23:59:59Z",
        "pages": None, "page_size": None,
    }

    batch_size = int(kwargs.get("db_batch_size") or 10)
    t0 = time.time()
    total_inserted = total_updated = total_skipped = 0


    #definición de formato de columnas(que datos se espera recibir en cada columna)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            for b in _iterate_batches(rows, batch_size):
                vals = []
                for row in b:
                    win_start = row.get("_win_start_utc", win["window_start_utc"])
                    win_end   = row.get("_win_end_utc",   win["window_end_utc"])
                    vals.append((
                        row["Id"],
                        json.dumps(row),
                        win_start,
                        win_end,
                        win.get("pages"),
                        win.get("page_size"),
                        json.dumps({
                            "env": "sandbox",
                            "source": "mage",
                            "minor_version": data.get("minor_version"),
                            "generated_at_utc": data.get("generated_at_utc"),
        }),
    ))


                template = "(" \
                           "%s, " \
                           "%s::jsonb, " \
                           "%s::timestamptz, %s::timestamptz, " \
                           "%s, %s, " \
                           "%s::jsonb" \
                           ")"

                ret = execute_values(
                    cur,
                    UPSERT,
                    vals,
                    template=template,
                    page_size=200,
                    fetch=True,
                )

                inserted = sum(1 for r in ret if r[0] is True)
                updated  = sum(1 for r in ret if r[1] is True)
                skipped  = len(b) - (inserted + updated)

                total_inserted += inserted
                total_updated  += updated
                total_skipped  += skipped

                print(f"Batch INVOICES: {len(b)} (inserted={inserted}, updated={updated}, skipped={skipped})")

        conn.commit()

    elapsed = round(time.time() - t0, 2)
    print(f"Carga INVOICES: {len(rows)} filas en {elapsed}s "
          f"(inserted={total_inserted}, updated={total_updated}, skipped={total_skipped})")

    return {
        "processed": len(rows),
        "inserted": total_inserted,
        "updaed": total_updated,
        "skipped": total_skipped,
        "elapsed_sec": elapsed,
        "finished_at_utc": _utc_iso_now(),
    }
