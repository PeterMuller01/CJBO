#!/usr/bin/env python3
"""Compare two scraper output files record by record.

Use it to check that a faster mode (for example ``--fetch-mode auto``) finds
the same data as a full browser run before switching to it:

    python scrape_charity_contacts.py --output run_browser.json
    python scrape_charity_contacts.py --fetch-mode auto --output run_auto.json
    python compare_outputs.py run_browser.json run_auto.json

Timestamps, IRS fields, and the fetch method are ignored because they are
expected to differ between runs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

IGNORED_TOP_LEVEL = {"scraped_at", "last_checked", "irs_status", "irs_last_checked", "irs_details"}
FLAT_FIELDS = (
    "org_name", "org_type", "category", "org_description", "area_served", "website_url",
    "org_phone", "hotline_24_7", "org_email", "street_address", "city", "state", "zip",
    "ein", "is_active", "org_notes",
)


def normalized(record: dict[str, Any]) -> dict[str, Any]:
    record = json.loads(json.dumps(record))
    for key in IGNORED_TOP_LEVEL:
        record.pop(key, None)
    record.get("field_sources", {}).pop("last_checked", None)
    record.get("extraction", {}).pop("fetch_method", None)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two scraper output files.")
    parser.add_argument("first")
    parser.add_argument("second")
    args = parser.parse_args()

    first = {r["org_id"]: r for r in json.loads(Path(args.first).read_text(encoding="utf-8"))}
    second = {r["org_id"]: r for r in json.loads(Path(args.second).read_text(encoding="utf-8"))}

    differing = 0
    for org_id in sorted(set(first) | set(second)):
        if org_id not in first or org_id not in second:
            print(f"{org_id}: only in {args.first if org_id in first else args.second}")
            differing += 1
            continue
        a, b = normalized(first[org_id]), normalized(second[org_id])
        if a == b:
            continue
        differing += 1
        flat = [field for field in FLAT_FIELDS if a.get(field) != b.get(field)]
        contacts = [key for key in a["contacts"] if a["contacts"].get(key) != b["contacts"].get(key)]
        status = (a["extraction"]["status"], b["extraction"]["status"])
        print(f"{org_id} {first[org_id]['org_name']}")
        if status[0] != status[1]:
            print(f"    status: {status[0]} -> {status[1]}")
        if flat:
            print(f"    import fields that differ: {', '.join(flat)}")
            for field in flat:
                print(f"        {field}: {a.get(field)!r} -> {b.get(field)!r}")
        if contacts:
            print(f"    contact lists that differ: {', '.join(contacts)}")
        if not (flat or contacts or status[0] != status[1]):
            print("    only review metadata differs (pages checked, sources, conflicts)")

    print(f"\n{len(first)} vs {len(second)} records; {differing} differ.")


if __name__ == "__main__":
    main()
