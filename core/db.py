"""
PostgreSQL connection helper — shared across ingest_corpus/ and ingest_public/.

Reads DATABASE_URL from the environment (set in .env for local dev;
Cloud SQL Auth Proxy exposes the instance on a local port).
"""

import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


def get_conn() -> psycopg2.extensions.connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable not set. See .env.example.")
    return psycopg2.connect(url)


def dict_cur(conn) -> psycopg2.extras.RealDictCursor:
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
