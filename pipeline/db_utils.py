"""
PostgreSQL connection helper for pipeline scripts.

Reads DATABASE_URL from the environment (set in .env for local dev;
Cloud SQL Auth Proxy exposes the instance on localhost:5432).

Usage:
    from db_utils import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ...")
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
    conn = psycopg2.connect(url)
    return conn


def dict_cur(conn) -> psycopg2.extras.RealDictCursor:
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
