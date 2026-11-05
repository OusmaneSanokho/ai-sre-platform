import logging
import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("orders-api")

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_NAME = os.getenv("DB_NAME", "shop")
DB_USER = os.getenv("DB_USER", "shop")
DB_PASSWORD = os.getenv("DB_PASSWORD", "shop")

app = FastAPI()


def get_conn():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=3,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/orders")
def orders():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS orders "
                    "(id SERIAL PRIMARY KEY, item TEXT NOT NULL)"
                )
                cur.execute("SELECT count(*) FROM orders")
                count = cur.fetchone()[0]
        log.info("orders request ok, count=%s", count)
        return {"orders": count}
    except psycopg.OperationalError as e:
        log.error("database connection failed: host=%s error=%s", DB_HOST, e)
        return JSONResponse(status_code=503, content={"error": "database unavailable"})