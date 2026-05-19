#!/usr/bin/env python3
"""
Geocoding sweep.

Reads `projects` with NULL lat/lng, calls a geocoder, and populates
lat / lng / county. Idempotent — re-running only geocodes rows that
are still missing coordinates.

Default backend is Google Maps Geocoding (returns county directly via
`address_components`, single API call per project, ~$5 / 1000). The
backend is pluggable so a future swap to the US Census Geocoder or
Mapbox doesn't touch the sweep logic.

Usage:
    python -m core.geocode.geocode             # all NULL-lat projects
    python -m core.geocode.geocode --limit 10  # cap requests
    python -m core.geocode.geocode --redo      # re-geocode everything
    python -m core.geocode.geocode --dry-run   # print would-do, no writes

Requires GOOGLE_MAPS_API_KEY for the default backend.
"""

import argparse
import json
import os
import sys
import time

import httpx
import tenacity
from tqdm import tqdm

from core.db import dict_cur, get_conn

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DEFAULT_THROTTLE_SEC = 0.05  # well under Google's 50 QPS default cap


class GeocoderError(RuntimeError):
    pass


# ── Google Maps backend ──────────────────────────────────────────────────────

@tenacity.retry(
    retry=tenacity.retry_if_exception_type((
        httpx.HTTPError, httpx.TransportError,
    )),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=30),
    stop=tenacity.stop_after_attempt(5),
)
def _google_geocode(query: str, *, api_key: str) -> dict | None:
    """Hit Google's geocoding endpoint. Returns the first result dict
    or None if no match. Raises GeocoderError on hard API errors
    (auth, billing, quota), which the caller should surface."""
    resp = httpx.get(
        GOOGLE_GEOCODE_URL,
        params={"address": query, "key": api_key},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    status = body.get("status")
    if status == "OK":
        return body["results"][0]
    if status == "ZERO_RESULTS":
        return None
    # OVER_QUERY_LIMIT, REQUEST_DENIED, INVALID_REQUEST etc. are not
    # retryable in tenacity's eyes — fail loud.
    raise GeocoderError(f"google geocode {status}: {body.get('error_message')}")


def _extract(result: dict) -> dict:
    """Pull lat/lng/county out of a Google result envelope."""
    geom = result.get("geometry", {}).get("location") or {}
    out = {"lat": geom.get("lat"), "lng": geom.get("lng"), "county": None}
    for comp in result.get("address_components") or []:
        if "administrative_area_level_2" in (comp.get("types") or []):
            # Strip the trailing " County" Google sometimes attaches.
            name = comp["long_name"]
            out["county"] = name.removesuffix(" County").strip() or None
            break
    return out


# ── Sweep ────────────────────────────────────────────────────────────────────

def _query_for_project(p: dict) -> str | None:
    """Build the geocode query string. Prefer location + city + state
    (most specific), fall back to city + state. Skip if neither yields
    a meaningful query — would just waste API calls on city='' state=''."""
    parts = [
        p.get("location"),
        p.get("city"),
        p.get("state"),
    ]
    parts = [s.strip() for s in parts if s and s.strip()]
    if not parts:
        return None
    return ", ".join(parts)


def sweep_projects(
    conn,
    *,
    api_key: str,
    limit: int | None = None,
    redo: bool = False,
    throttle_sec: float = DEFAULT_THROTTLE_SEC,
    dry_run: bool = False,
) -> dict:
    """Geocode projects with NULL lat or lng (or all, with `redo`).
    Returns counts per outcome."""
    where = "TRUE" if redo else "(lat IS NULL OR lng IS NULL)"
    sql = f"""
        SELECT id, name, location, city, state, county, lat, lng
        FROM projects
        WHERE {where}
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    with dict_cur(conn) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    stats = {"geocoded": 0, "no_match": 0, "no_query": 0, "skipped": 0}

    for row in tqdm(rows, desc="projects"):
        query = _query_for_project(row)
        if not query:
            stats["no_query"] += 1
            continue

        result = _google_geocode(query, api_key=api_key)
        if not result:
            stats["no_match"] += 1
            continue
        extracted = _extract(result)

        if dry_run:
            tqdm.write(f"  {row['name']}: {query!r} -> {extracted}")
            stats["geocoded"] += 1
            continue

        with dict_cur(conn) as cur:
            cur.execute(
                """
                UPDATE projects
                SET lat    = COALESCE(%s, lat),
                    lng    = COALESCE(%s, lng),
                    county = COALESCE(county, %s)
                WHERE id = %s
                """,
                (extracted["lat"], extracted["lng"], extracted["county"], row["id"]),
            )
        conn.commit()
        stats["geocoded"] += 1

        if throttle_sec > 0:
            time.sleep(throttle_sec)

    return stats


def main():
    p = argparse.ArgumentParser(description="Geocode projects with NULL lat/lng.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--redo", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--throttle-sec", type=float, default=DEFAULT_THROTTLE_SEC)
    args = p.parse_args()

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_MAPS_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    conn = get_conn()
    try:
        stats = sweep_projects(
            conn, api_key=api_key, limit=args.limit, redo=args.redo,
            throttle_sec=args.throttle_sec, dry_run=args.dry_run,
        )
    finally:
        conn.close()
    print(json.dumps(stats, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
