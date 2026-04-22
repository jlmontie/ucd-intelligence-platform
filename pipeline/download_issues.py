#!/usr/bin/env python3
"""
Download all UCD magazine issues from the archive page.

Usage:
    python download_issues.py
    python download_issues.py --output issues/ --no-skip-existing
"""

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

ARCHIVE_URL = "https://www.utahcdmag.com/archive"
CDN_BASE = "irp.cdn-website.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


def fetch_pdf_links(session: requests.Session) -> list[dict]:
    print(f"Fetching archive page: {ARCHIVE_URL}")
    resp = session.get(ARCHIVE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    links = []

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if CDN_BASE in href and href.lower().endswith(".pdf"):
            filename = unquote(urlparse(href).path.split("/")[-1])
            links.append({"url": href, "filename": filename})

    # Deduplicate by URL
    seen = set()
    unique = []
    for link in links:
        if link["url"] not in seen:
            seen.add(link["url"])
            unique.append(link)

    print(f"Found {len(unique)} unique PDF links")

    return unique


def download_pdf(session: requests.Session, url: str, dest: Path) -> bool:
    try:
        resp = session.get(url, headers=HEADERS, stream=True, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        size_kb = dest.stat().st_size // 1024
        print(f"  OK ({size_kb} KB)")
        return True
    except requests.RequestException as e:
        print(f"  FAILED: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Download UCD magazine issues")
    parser.add_argument("--output", "-o", default="issues", help="Output directory (default: issues/)")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Skip files already downloaded (default: true)")
    parser.add_argument("--no-skip-existing", dest="skip_existing", action="store_false",
                        help="Re-download even if file exists")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds to wait between downloads (default: 1.0)")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    links = fetch_pdf_links(session)
    if not links:
        print("ERROR: No PDF links found. The page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    print()

    ok, skipped, failed = 0, 0, 0
    for i, link in enumerate(links, 1):
        dest = out_dir / link["filename"]
        print(f"[{i}/{len(links)}] {link['filename']}")

        if args.skip_existing and dest.exists():
            print("  skipped (already exists)")
            skipped += 1
            continue

        success = download_pdf(session, link["url"], dest)
        if success:
            ok += 1
        else:
            failed += 1

        if i < len(links):
            time.sleep(args.delay)

    print(f"\nDone: {ok} downloaded, {skipped} skipped, {failed} failed")
    print(f"Files saved to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
