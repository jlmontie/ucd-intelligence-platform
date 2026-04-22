#!/usr/bin/env python3
"""
Extract construction project info boxes from UCD magazine PDFs.

Usage:
    python extract_projects.py --issues_dir issues/
    python extract_projects.py --pdfs issues/UC-D+February+2026-spreads.pdf

Each PDF page is scanned for labeled fields (Location:, Owner:, Architect:, etc.).
Pages with enough matching labels are sent to an LLM for structured extraction.

Per-issue results are written to --extracted_dir (default: extracted/) as individual
JSON files. Already-extracted issues are skipped, making re-runs safe and fast.
A merged --output file (default: projects.json) is written at the end.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import litellm
import pdfplumber
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

litellm.suppress_debug_info = True

INFO_BOX_SIGNALS = [
    r"Location\s*:",
    r"Owner\s*:",
    r"Architect\s*:",
    r"General Contractor\s*:",
    r"Square F(?:eet|ootage)\s*:",
    r"Design Team",
    r"Construction Team",
    r"Project Team",
    r"Structural Engineer\s*:",
    r"Mechanical Engineer\s*:",
    r"Electrical Engineer\s*:",
    r"Civil Engineer\s*:",
    r"Cost\s*:",
    r"Developer\s*:",
]

DEFAULT_THRESHOLD = 3


def score_page(text: str) -> int:
    if not text:
        return 0
    return sum(1 for p in INFO_BOX_SIGNALS if re.search(p, text, re.IGNORECASE))


def extract_info_boxes_from_page(
    model: str, page_text: str, filename: str, page_num: int
) -> list[dict]:
    prompt = f"""You are extracting construction project information from a magazine page.

The following text comes from page {page_num} of "{filename}". It may contain one or more project info panels/boxes mixed in with article body text.

Extract each project info box you find and return a JSON array. Each element should be an object with these fields (use null for missing fields):
- project_name: string
- location: string
- cost: string (preserve original formatting, e.g. "$45,900,000")
- delivery_method: string
- stories_levels: string
- square_footage: string
- year_completed: string (if mentioned)
- owner: string
- owner_rep: string
- developer: string
- design_team: object with keys: architect, structural_engineer, mechanical_engineer, electrical_engineer, civil_engineer, interior_design, landscape_architect, geotech_engineer, lighting_design, food_service_design, furniture, other (array of "Role: Firm" strings for anything not covered)
- construction_team: object with keys: general_contractor, plumbing, hvac, electrical, concrete, steel_fabrication, steel_erection, glass_curtain_wall, masonry, drywall_acoustics, painting, tile_stone, carpentry, flooring, roofing, waterproofing, excavation, demolition, landscaping, millwork, other (array of "Role: Firm" strings for anything not covered). If a role has multiple firms, join them with " / ".

Rules:
- Only extract data from structured info box/project panel — ignore firm/people mentions in article prose.
- If a role appears multiple times (e.g. two concrete subs), include both in "other" as "Concrete: Firm1" and "Concrete: Firm2".
- Return ONLY a valid JSON array, no commentary. If no info box found, return [].

PAGE TEXT:
{page_text}"""

    response = litellm.completion(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError as e:
        tqdm.write(f"  WARNING: JSON parse error on page {page_num}: {e}")
        tqdm.write(f"  Raw response: {raw[:200]}")
        return []


def process_pdf(
    pdf_path: Path,
    model: str,
    threshold: int,
    extracted_dir: Path,
    issue_bar: tqdm,
) -> list[dict]:
    out_file = extracted_dir / f"{pdf_path.stem}.json"

    if out_file.exists():
        existing = json.loads(out_file.read_text())
        issue_bar.write(f"  SKIP {pdf_path.name} (already extracted: {len(existing)} project(s))")
        return existing

    results = []

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        with tqdm(
            total=len(pages),
            desc=f"  {pdf_path.name[:45]}",
            unit="pg",
            leave=False,
            file=sys.stdout,
        ) as page_bar:
            for i, page in enumerate(pages, start=1):
                text = page.extract_text() or ""
                score = score_page(text)
                page_bar.update(1)

                if score < threshold:
                    continue

                page_bar.write(f"    page {i} (score={score}) → {model}...")
                boxes = extract_info_boxes_from_page(model, text, pdf_path.name, i)

                for box in boxes:
                    if box:
                        box["source_file"] = pdf_path.name
                        box["source_page"] = i
                        results.append(box)
                        page_bar.write(f"      + {box.get('project_name') or '(unnamed)'}")

    out_file.write_text(json.dumps(results, indent=2))
    issue_bar.write(f"  DONE {pdf_path.name} → {len(results)} project(s) → {out_file.name}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Extract project info boxes from UCD magazine PDFs")
    parser.add_argument("--pdfs", nargs="+", help="PDF file(s) to process")
    parser.add_argument("--issues_dir", default=None, help="Directory of PDF issues to process (e.g. issues/)")
    parser.add_argument("--extracted_dir", default="extracted", help="Directory for per-issue JSON output (default: extracted/)")
    parser.add_argument("--output", "-o", default="projects.json", help="Merged output JSON file (default: projects.json)")
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"Min signal count to flag a page (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument("--model", default="claude-opus-4-7",
                        help="LiteLLM model string (default: claude-opus-4-7). "
                             "Examples: gpt-4o, gemini/gemini-2.0-flash, claude-sonnet-4-6")
    parser.add_argument("--reprocess", action="store_true", help="Ignore cached extractions and reprocess all PDFs")
    args = parser.parse_args()

    extracted_dir = Path(args.extracted_dir)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    if args.reprocess:
        for f in extracted_dir.glob("*.json"):
            f.unlink()

    if args.issues_dir:
        pdf_paths = sorted(Path(args.issues_dir).glob("*.pdf"))
        if not pdf_paths:
            print(f"ERROR: No PDFs found in {args.issues_dir}", file=sys.stderr)
            sys.exit(1)
    elif args.pdfs:
        pdf_paths = []
        for p in args.pdfs:
            path = Path(p)
            if not path.exists():
                print(f"WARNING: File not found: {path}", file=sys.stderr)
            else:
                pdf_paths.append(path)
    else:
        print("ERROR: Provide --issues_dir or --pdfs.", file=sys.stderr)
        sys.exit(1)

    all_results = []

    with tqdm(pdf_paths, desc="Issues", unit="issue", file=sys.stdout) as issue_bar:
        for pdf_path in issue_bar:
            issue_bar.set_postfix_str(pdf_path.name[:40])
            results = process_pdf(pdf_path, args.model, args.threshold, extracted_dir, issue_bar)
            all_results.extend(results)

    Path(args.output).write_text(json.dumps(all_results, indent=2))
    print(f"\nDone. {len(all_results)} total project(s) → {args.output}")


if __name__ == "__main__":
    main()
