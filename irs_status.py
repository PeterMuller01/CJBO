#!/usr/bin/env python3
"""Check each organization's EIN against IRS bulk data.

Two IRS datasets are used:

* Exempt Organizations Business Master File (EO BMF) -- every organization the
  IRS currently recognizes as tax-exempt, nationwide (four regional CSV files).
* Automatic Revocation of Exemption List -- organizations that lost their
  exemption for not filing a return for three consecutive years.

The result is written to new fields and never changes ``is_active``:

* ``irs_status``        active | revoked | not_found | no_ein | invalid_ein | not_checked
* ``irs_last_checked``  date (YYYY-MM-DD) this check ran
* ``irs_details``       what the IRS files say, plus reasons to review the record

Downloads are cached in ``--cache-dir`` and refreshed with a conditional
request, so repeat runs do not re-download unchanged files.

Examples:
    # Add IRS fields to an existing scrape output (writes the file in place).
    python irs_status.py --input charity_contacts_live.json

    # Write to a separate file instead.
    python irs_status.py --input charity_contacts_live.json --output checked.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

BMF_URLS = tuple(f"https://www.irs.gov/pub/irs-soi/eo{region}.csv" for region in range(1, 5))
REVOCATION_URL = "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip"
USER_AGENT = "charity-contact-scraper/1.0 (IRS exempt organization status check)"

# Columns in the auto-revocation list (pipe-delimited, no header row).
REVOCATION_COLUMNS = (
    "ein", "legal_name", "dba_name", "address", "city", "state", "zip", "country",
    "exemption_type", "revocation_date", "revocation_posting_date", "reinstatement_date",
)


@dataclass
class IrsData:
    bmf_files: list[Path]
    revocation_file: Path
    data_as_of: str | None
    warnings: list[str] = field(default_factory=list)


def ein_digits(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 9 else None


def format_ein(digits: str) -> str:
    return f"{digits[:2]}-{digits[2:]}"


def _http_date(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def download(url: str, cache_dir: Path, max_age_hours: float, session: Any) -> tuple[Path, str | None, str | None]:
    """Fetch ``url`` into the cache, skipping it when the cached copy is fresh.

    Returns (path, last_modified, warning).  A failed refresh falls back to the
    cached copy so a temporary IRS outage does not stop the run.
    """
    target = cache_dir / url.rsplit("/", 1)[1]
    meta_path = target.with_name(target.name + ".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() and target.exists() else {}

    fetched_at = _http_date(meta.get("fetched_at_http"))
    if fetched_at and (datetime.now(timezone.utc) - fetched_at).total_seconds() < max_age_hours * 3600:
        return target, meta.get("last_modified"), None

    headers = {}
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    try:
        with session.get(url, headers=headers, stream=True, timeout=60) as response:
            if response.status_code == 304:
                meta["fetched_at_http"] = _now_http()
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
                return target, meta.get("last_modified"), None
            response.raise_for_status()
            partial = target.with_name(target.name + ".part")
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    handle.write(chunk)
            partial.replace(target)
            meta = {
                "url": url,
                "last_modified": response.headers.get("Last-Modified"),
                "etag": response.headers.get("ETag"),
                "fetched_at_http": _now_http(),
            }
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            return target, meta["last_modified"], None
    except Exception as exc:
        if target.exists():
            return target, meta.get("last_modified"), f"Could not refresh {url} ({type(exc).__name__}); used cached copy."
        raise RuntimeError(f"Could not download {url}: {type(exc).__name__}: {exc}") from exc


def _now_http() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def ensure_irs_files(cache_dir: Path, max_age_hours: float = 24.0) -> IrsData:
    """Download (or reuse) the four BMF regions and the revocation list in parallel."""
    import requests

    cache_dir.mkdir(parents=True, exist_ok=True)
    urls = (*BMF_URLS, REVOCATION_URL)

    def fetch(url: str):
        with requests.Session() as session:
            session.headers["User-Agent"] = USER_AGENT
            return download(url, cache_dir, max_age_hours, session)

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        results = list(pool.map(fetch, urls))

    stamps = [_http_date(modified) for _, modified, _ in results]
    newest = max((stamp for stamp in stamps if stamp), default=None)
    return IrsData(
        bmf_files=[path for path, _, _ in results[:-1]],
        revocation_file=results[-1][0],
        data_as_of=newest.date().isoformat() if newest else None,
        warnings=[warning for _, _, warning in results if warning],
    )


def scan_bmf(paths: Iterable[Path], wanted: set[str]) -> dict[str, dict[str, str]]:
    """Return BMF rows for the wanted EINs.

    The EIN is the first column, so each line is checked by prefix before any
    CSV parsing; only the handful of matching lines are fully parsed.
    """
    found: dict[str, dict[str, str]] = {}
    for path in paths:
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            header = next(csv.reader([handle.readline()]))
            header = [column.strip().upper() for column in header]
            for line in handle:
                key = line[1:10] if line.startswith('"') else line[:9]
                if key in wanted and key not in found:
                    row = next(csv.reader([line]))
                    found[key] = dict(zip(header, (value.strip() for value in row)))
    return found


def scan_revocations(path: Path, wanted: set[str]) -> dict[str, list[dict[str, str]]]:
    """Return every revocation-list row for the wanted EINs (an EIN can repeat)."""
    found: dict[str, list[dict[str, str]]] = {}

    def lines() -> Iterable[str]:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    with archive.open(name) as raw:
                        yield from io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        else:
            with path.open(encoding="utf-8", errors="replace") as handle:
                yield from handle

    for line in lines():
        key = ein_digits(line.split("|", 1)[0])
        if key in wanted:
            values = [value.strip() for value in line.rstrip("\r\n").split("|")]
            found.setdefault(key, []).append(dict(zip(REVOCATION_COLUMNS, values)))
    return found


def parse_irs_date(value: str | None) -> str | None:
    value = (value or "").strip()
    for pattern in ("%d-%b-%Y", "%m/%d/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def yyyymm(value: str | None) -> str | None:
    value = (value or "").strip()
    return f"{value[:4]}-{value[4:6]}" if re.fullmatch(r"\d{6}", value) else None


def names_match(first: str, second: str) -> bool:
    """Loose name comparison; IRS legal names often differ from public names."""
    stop = {"inc", "incorporated", "the", "of", "and", "for", "corp", "corporation", "co", "ltd", "llc"}

    def words(value: str) -> set[str]:
        # The IRS drops apostrophes ("Children's" is filed as "CHILDRENS").
        value = re.sub(r"['’`]", "", value.lower())
        return {word for word in re.findall(r"[a-z0-9]+", value) if word not in stop}

    left, right = words(first), words(second)
    if not left or not right:
        return False
    shared = left & right
    return shared == left or shared == right or len(shared) >= 2


def evaluate(record: dict[str, Any], bmf: dict[str, dict[str, str]],
             revocations: dict[str, list[dict[str, str]]]) -> tuple[str, dict[str, Any]]:
    """Decide irs_status for one record and explain how."""
    raw_ein = str(record.get("ein") or "").strip()
    details: dict[str, Any] = {"ein_checked": raw_ein or None, "review_reasons": []}
    reasons = details["review_reasons"]

    if not raw_ein:
        reasons.append("No EIN on file (government agencies and some programs do not have one).")
        return "no_ein", details
    key = ein_digits(raw_ein)
    if not key:
        reasons.append(f"EIN {raw_ein!r} is not nine digits.")
        return "invalid_ein", details
    details["ein_checked"] = format_ein(key)

    original = (record.get("original_fields") or {}).get("ein")
    if original and ein_digits(original) and ein_digits(original) != key:
        reasons.append(f"Scraped EIN differs from the provided EIN {original}.")

    row = bmf.get(key)
    details["bmf_listed"] = row is not None
    if row:
        details.update(
            irs_name=row.get("NAME") or None,
            irs_city=row.get("CITY") or None,
            irs_state=row.get("STATE") or None,
            subsection=row.get("SUBSECTION") or None,
            exempt_status_code=row.get("STATUS") or None,
            deductibility_code=row.get("DEDUCTIBILITY") or None,
            ruling_date=yyyymm(row.get("RULING")),
            latest_tax_period=yyyymm(row.get("TAX_PERIOD")),
            group_exemption_number=(row.get("GROUP") or "").lstrip("0") or None,
        )
        # The scrape can replace org_name, so accept a match on either name.
        known_names = (record.get("org_name", ""), (record.get("original_fields") or {}).get("org_name", ""))
        if row.get("NAME") and not any(names_match(name, row["NAME"]) for name in known_names if name):
            reasons.append("IRS legal name does not resemble org_name; confirm the EIN.")

    revoked_rows = revocations.get(key, [])
    revocation_dates = [parse_irs_date(r.get("revocation_date")) for r in revoked_rows]
    reinstatement_dates = [parse_irs_date(r.get("reinstatement_date")) for r in revoked_rows]
    last_revoked = max((d for d in revocation_dates if d), default=None)
    last_reinstated = max((d for d in reinstatement_dates if d), default=None)
    currently_revoked = bool(revoked_rows) and not (
        last_reinstated and (not last_revoked or last_reinstated >= last_revoked)
    )
    details.update(
        revoked=currently_revoked,
        revocation_date=last_revoked,
        reinstatement_date=last_reinstated,
    )
    if revoked_rows and not last_revoked:
        details["revocation_date_raw"] = revoked_rows[-1].get("revocation_date")

    if currently_revoked:
        status = "revoked"
        reasons.append("EIN is on the IRS automatic revocation list with no later reinstatement.")
        if row:
            reasons.append("EIN is also still in the Business Master File; the IRS files disagree.")
    elif row:
        status = "active"
        if last_reinstated:
            reasons.append(f"Exemption was revoked and later reinstated ({last_reinstated}).")
    else:
        status = "not_found"
        reasons.append(
            "EIN is not in the Business Master File. Possible causes: wrong EIN, a church or "
            "chapter covered by a parent's group exemption, or a non-exempt organization."
        )
    return status, details


def apply_irs_status(records: list[dict[str, Any]], data: IrsData | None, error: str | None = None,
                     checked_on: str | None = None) -> list[dict[str, Any]]:
    """Add irs_status / irs_last_checked / irs_details to each record in place."""
    checked_on = checked_on or date.today().isoformat()
    bmf: dict[str, dict[str, str]] = {}
    revocations: dict[str, list[dict[str, str]]] = {}
    if data:
        wanted = {key for record in records for key in (
            ein_digits(record.get("ein")),
            ein_digits((record.get("original_fields") or {}).get("ein")),
        ) if key}
        bmf = scan_bmf(data.bmf_files, wanted)
        revocations = scan_revocations(data.revocation_file, wanted)

    for index, record in enumerate(records):
        if data:
            status, details = evaluate(record, bmf, revocations)
            details["irs_data_as_of"] = data.data_as_of
            details["sources"] = [*BMF_URLS, REVOCATION_URL]
            if data.warnings:
                details["warnings"] = list(data.warnings)
            last_checked = checked_on
        else:
            status, last_checked = "not_checked", None
            details = {"ein_checked": record.get("ein") or None,
                       "review_reasons": [f"IRS data was unavailable: {error or 'not downloaded'}"]}
        records[index] = with_irs_fields(record, status, last_checked, details)
    return records


def with_irs_fields(record: dict[str, Any], status: str, last_checked: str | None,
                    details: dict[str, Any]) -> dict[str, Any]:
    """Place the IRS fields right after is_active so the flat columns stay together."""
    output: dict[str, Any] = {}
    for key, value in record.items():
        if key in ("irs_status", "irs_last_checked", "irs_details"):
            continue
        output[key] = value
        if key == "is_active":
            output.update(irs_status=status, irs_last_checked=last_checked)
    if "irs_status" not in output:
        output.update(irs_status=status, irs_last_checked=last_checked)
    output["irs_details"] = details
    return output


def load_irs_data(cache_dir: Path, max_age_hours: float) -> tuple[IrsData | None, str | None]:
    try:
        return ensure_irs_files(cache_dir, max_age_hours), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def summarize(records: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["irs_status"]] = counts.get(record["irs_status"], 0) + 1
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Add IRS exempt-status fields to a charity JSON file.")
    parser.add_argument("--input", default="charity_contacts_live.json", help="JSON list of organization records.")
    parser.add_argument("--output", help="Output path (default: overwrite --input).")
    parser.add_argument("--cache-dir", default="irs_data", help="Where IRS downloads are cached (default: irs_data).")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="Reuse cached IRS files younger than this without contacting the IRS (default: 24).")
    args = parser.parse_args()

    input_path = Path(args.input)
    records = json.loads(input_path.read_text(encoding="utf-8"))
    data, error = load_irs_data(Path(args.cache_dir), args.max_age_hours)
    if error:
        print(f"WARNING: {error}")
    apply_irs_status(records, data, error)
    output_path = Path(args.output) if args.output else input_path
    output_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"IRS status for {len(records)} records ({summarize(records)}) written to {output_path}")


if __name__ == "__main__":
    main()
