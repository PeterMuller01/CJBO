#!/usr/bin/env python3
"""Collect public charity organization profiles and contacts with Botasaurus.

The script accepts either the supplied Markdown table or a cleaned JSON target
list.  Every valid public address, phone number, and email it finds is kept
with a score; the top-graded one of each fills the main database fields.

Examples:
    # Create a portable target file from the supplied Markdown table.
    python scrape_charity_contacts.py --input "Pasted markdown(2).md" \
        --export-targets --output charity_scrape_targets.json

    # Build a JSON inventory from values already documented in the table.
    python scrape_charity_contacts.py --input charity_scrape_targets.json \
        --inventory-only --output charity_contact_inventory.json

    # Do the live Botasaurus scrape.
    python scrape_charity_contacts.py --input charity_scrape_targets.json \
        --output charity_contacts_live.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse


# These programs provide confidential shelter locations.  Do not infer or
# publish addresses for them, even when an address-looking value appears.
PRIVATE_ADDRESS_ORG_IDS = {"CF-036", "CF-037"}

# The supplied notes flag these sites/pages as likely to need manual review.
KNOWN_MANUAL_REVIEW_IDS = {
    "CF-003",
    "CF-006",
    "CF-007",
    "CF-011",
    "CF-039",
    "CF-041",
    "CF-057",
}

PLACEHOLDER_VALUES = {
    "",
    "not found",
    "not listed",
    "hidden on site",
    "contact form only",
    "n/a",
    "none",
    "null",
    "-",
}

CONTACT_KEYWORDS = (
    "contact",
    "location",
    "locations",
    "office",
    "offices",
    "visit",
    "find us",
    "get in touch",
    "directions",
)

STREET_SUFFIXES = (
    r"street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|"
    r"drive|dr\.?|lane|ln\.?|court|ct\.?|circle|cir\.?|parkway|pkwy\.?|"
    r"highway|hwy\.?|way|place|pl\.?|square|sq\.?|trail|trl\.?|"
    r"terrace|ter\.?|loop|center|centre|plaza|pike|route|rte\.?"
)
ADDRESS_RE = re.compile(
    rf"\b(?:P\.?\s*O\.?\s*Box\s+\d+|"
    rf"\d{{1,6}}\s+(?:[A-Za-z0-9#.'’&-]+\s+){{1,8}}?(?:{STREET_SUFFIXES}))"
    rf"(?:\s*,?\s*(?:Suite|Ste\.?|Unit|Building|Bldg\.?|Floor|Fl\.?|#)\s*[A-Za-z0-9-]+)?"
    rf"(?:\s*,?\s*[A-Za-z .'-]+,\s*[A-Z]{{2}}\s*\d{{5}}(?:-\d{{4}})?)?",
    re.IGNORECASE,
)
PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}"
    r"(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)


# org_id, org_name, and website_url always come from the target file.  These
# profile fields come from the target file too, unless it leaves them blank;
# then the best value found on the website is used.
FIELDS = ('category', 'org_description')
PROFILE_WORDS = ('about', 'mission', 'who we are', 'services', 'service area', 'where we serve')


def text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def host(url):
    return urlparse(url).netloc.lower().removeprefix('www.')


def add(result, field, value, url, method, priority=50):
    value = text(value)
    if not value:
        return
    candidate = dict(value=value, source_url=url, method=method, priority=priority)
    bucket = result['organization_details'][field]
    if not any(row['value'].casefold() == value.casefold() for row in bucket):
        bucket.append(candidate)


def initialize(result):
    result['organization_details'] = {field: [] for field in FIELDS}
    result['original_fields'] = {k: result[k] for k in (
        'org_name', 'website_url', *FIELDS, 'org_phone', 'org_email', 'street_address',
        'city', 'state', 'zip', 'is_active', 'last_checked')}
    result['field_sources'] = {
        field: dict(value=value, source_url=None,
                    method='provided_inventory' if value else 'not_found')
        for field, value in result['original_fields'].items()
    }


def place_names(value):
    if isinstance(value, list):
        return '; '.join(filter(None, (place_names(v) for v in value)))
    if isinstance(value, dict):
        return text(value.get('name') or value.get('addressLocality') or value.get('addressRegion'))
    return text(value)


def structured(result, obj, url):
    """Caller has already matched this entity to the target organization."""
    for field, key in [('org_description', 'description'), ('category', 'category')]:
        value = place_names(obj.get(key))
        add(result, field, value[:600] if field == 'org_description' else value,
            url, 'matched_json_ld', 100)


def extract(result, soup, url):
    is_home = urlparse(url).path.rstrip('/') == ''
    if is_home:
        description = soup.select_one('meta[name="description"], meta[property="og:description"]')
        if description:
            add(result, 'org_description', text(description.get('content'))[:600], url, 'homepage_description', 70)
    # Restrict statements to readable paragraphs and small footer blocks.
    blocks = []
    for element in soup.select('main p, article p, [class*="about"] p, [class*="mission"] p, footer p, footer small, footer'):
        if element.find_parent(['nav', 'form', 'script', 'style']):
            continue
        value = text(element.get_text(' ', strip=True))
        if 10 <= len(value) <= 1200 and value not in blocks:
            blocks.append(value)
    if not soup.select_one('main, article'):
        for element in soup.select('body > p, body > section p'):
            value = text(element.get_text(' ', strip=True))
            if 10 <= len(value) <= 1200 and value not in blocks:
                blocks.append(value)
    name = re.escape(text(result['original_fields']['org_name']))
    subject = rf'(?:we|our (?:organization|agency|center)|{name})' if name else r'(?:we|our organization)'
    mission_blocks = []
    for block in blocks:
        # Require a self-description instead of references to partner charities.
        self_statement = re.search(rf'\b{subject}\s+(?:is|are|provides?|offers?|serves?|supports?|helps?|works?|operates?)\b', block, re.I)
        mission = re.search(r'\bour mission\b', block, re.I)
        if self_statement or mission:
            mission_blocks.append(block)
            if len(block) >= 40:
                add(result, 'org_description', block[:600], url, 'self_description_text', 60)
    # Category classification is explicitly tagged as a rule-based inference.
    rules = [
        ('Domestic Violence / Sexual Assault', r'domestic violence|sexual (?:assault|violence)|intimate partner violence'),
        ('Food Assistance', r'food (?:bank|pantry|assistance)|hunger relief'),
        ('Housing / Shelter', r'homelessness|homeless shelter|housing assistance'),
        ('Legal Assistance', r'legal aid|legal assistance|legal representation'),
        ('Healthcare', r'health ?care|medical care|health clinic'),
        ('Education', r'educational programs|adult literacy|education assistance'),
    ]
    if mission_blocks:
        matched = [label for label, pattern in rules if re.search(pattern, ' '.join(mission_blocks), re.I)]
        if matched:
            add(result, 'category', '; '.join(matched), url, 'inferred_from_self_description', 40)


def profile_pages(soup, url, root):
    pages = []
    for anchor in soup.select('a[href]'):
        value = urljoin(url, text(anchor.get('href'))).split('#', 1)[0]
        label = text(anchor.get_text(' ', strip=True)).lower()
        if urlparse(value).scheme not in ('http', 'https') or host(value) != host(root) or value == url:
            continue
        if is_listing_page(value, label):
            continue
        if any(word in f'{urlparse(value).path.lower()} {label}' for word in PROFILE_WORDS):
            if value not in pages and not re.search(r'\.(pdf|jpg|png|zip)$', urlparse(value).path, re.I):
                pages.append(value)
    return pages


# A scrape with these statuses read the website, so the charity is marked active.
ACTIVE_STATUSES = ('success', 'partial')
UNCHECKED_ACTIVE = 'Unsuccessful check, manual review'


def source_of(row, key):
    """Where a flat field's value came from, including its grade."""
    return {'value': row[key], 'source_url': row.get('source_url'), 'method': row.get('method'),
            **{k: row[k] for k in ('score', 'confidence') if k in row}}


# Sites word their own description several ways, so that is not a conflict.
CONFLICT_FIELDS = tuple(field for field in FIELDS if field != 'org_description')


def comparable(value):
    words = re.findall(r'[a-z0-9]+', re.sub(r"['’]", '', value.casefold()))
    return ' '.join(word for word in words if word not in {'inc', 'the', 'of', 'and'})


def has_conflict(rows):
    return len({comparable(row['value']) for row in rows} - {''}) > 1


def finalize(result, max_values=None):
    """Promote best profile evidence; keep source values as documented fallbacks."""
    for field in FIELDS:
        baseline = result['original_fields'][field]
        candidates = result['organization_details'][field]
        if candidates and not usable_value(baseline):
            # The target file left this blank, so use the best website value.
            # Website values always stay in organization_details for review.
            chosen = max(candidates, key=lambda row: row['priority'])
            result[field] = chosen['value']
            result['field_sources'][field] = {k: v for k, v in chosen.items() if k != 'priority'}
        else:
            result[field] = baseline
            result['field_sources'][field] = dict(
                value=baseline, source_url=None,
                method='provided_inventory' if baseline else 'not_found')
    # The primary (top-graded) entry fills each flat database field; every
    # graded entry stays in contacts with its score and reasons.
    for field, bucket, key in [('org_phone', 'phones', 'raw'),
                               ('org_email', 'emails', 'value')]:
        values = result['contacts'][bucket]
        if values:
            best = next((row for row in values if row.get('primary')), values[0])
            result[field] = best[key]
            result['field_sources'][field] = source_of(best, key)
        else:
            result[field] = result['original_fields'][field]
            result['field_sources'][field] = dict(value=result[field], source_url=None,
                method='provided_inventory' if result[field] else 'not_found')
    addresses = result['contacts']['addresses']
    if not addresses:
        for field in ('street_address', 'city', 'state', 'zip'):
            result[field] = result['original_fields'][field]
            result['field_sources'][field] = dict(value=result[field], source_url=None,
                method='provided_inventory' if result[field] else 'not_found')
    if addresses:
        best = (next((row for row in addresses if row.get('primary')), None)
                or next((row for row in addresses if row['kind'] == 'physical'), addresses[0]))
        raw = best['raw']
        locality = re.match(r'^(.*?),\s*([A-Za-z .\'-]+),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', raw)
        if locality:
            for field, value in zip(('street_address', 'city', 'state', 'zip'), locality.groups()):
                result[field] = value.strip()
                result['field_sources'][field] = {**source_of(best, 'raw'), 'value': value.strip()}
        else:
            # Do not combine a newly found street with a stale city/ZIP.
            result['street_address'] = raw
            result['city'] = result['state'] = result['zip'] = ''
            result['field_sources']['street_address'] = dict(best)
            for field in ('city', 'state', 'zip'):
                result['field_sources'][field] = dict(value='', source_url=None, method='not_parsed')
    result['extraction']['profile_missing_on_website'] = [f for f in FIELDS if not result['organization_details'][f]]
    result['extraction']['profile_review_required'] = any(
        row['method'].startswith('inferred') for row in result['field_sources'].values())
    result['extraction']['profile_conflicts'] = [
        field for field in CONFLICT_FIELDS if has_conflict(result['organization_details'][field])]
    if result['extraction']['profile_conflicts']:
        result['extraction']['profile_review_required'] = True
    if max_values:
        for candidates in result['organization_details'].values():
            candidates.sort(key=lambda row: -row['priority'])
            del candidates[max_values:]
    if result['data_origin'] == 'live_scrape':
        if result['extraction']['pages_checked']:
            result['last_checked'] = result['scraped_at'][:10]
            result['field_sources']['last_checked'] = dict(
                value=result['last_checked'], source_url=None, method='website_check_date')
        # Active status follows whether this check could read the website; a
        # failed check is left for a person, since the site may just be down.
        status = result['extraction']['status']
        result['is_active'] = 'Yes' if status in ACTIVE_STATUSES else UNCHECKED_ACTIVE
        result['field_sources']['is_active'] = dict(
            value=result['is_active'], source_url=None, method=f'scrape_status_{status}')
    if result['org_id'] in PRIVATE_ADDRESS_ORG_IDS:
        for fields in (result, result['original_fields']):
            for field in ('street_address', 'city', 'state', 'zip'):
                fields[field] = ''
        for field in ('street_address', 'city', 'state', 'zip'):
            result['field_sources'][field] = dict(value='', source_url=None, method='confidential_location_omitted')



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    """Normalize markdown-table spacing and escaped email signs."""
    if value is None:
        return ""
    text = str(value).replace(r"\@", "@").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def usable_value(value: Any) -> str | None:
    cleaned = clean_text(value)
    return None if cleaned.lower() in PLACEHOLDER_VALUES else cleaned


def markdown_url(value: Any) -> str | None:
    """Return a Markdown link destination (or a plain HTTP URL) from a table cell."""
    text = clean_text(value)
    match = re.search(r"\]\((https?://[^)]+)\)", text)
    if match:
        return match.group(1).strip()
    return text if text.startswith(("https://", "http://")) else None


def keyify(value: str) -> str:
    return clean_text(value).lower().replace(" ", "_")


def parse_markdown_table(path: Path) -> list[dict[str, str]]:
    """Read the attached pipe-delimited Markdown table without extra packages."""
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) < 3:
        raise ValueError(f"{path} does not contain a Markdown table.")

    def cells(line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [clean_text(cell) for cell in stripped.split("|")]

    headers = [keyify(header) for header in cells(rows[0])]
    records: list[dict[str, str]] = []
    for line in rows[2:]:
        values = cells(line)
        if len(values) < len(headers):
            values.extend([""] * (len(headers) - len(values)))
        record = dict(zip(headers, values[: len(headers)]))
        if record.get("org_id", "").startswith("CF-"):
            records.append(record)
    if not records:
        raise ValueError(f"No organization rows were found in {path}.")
    return records


# The scraper input (charity_scrape_targets.json) holds only these fields.
TARGET_FIELDS = (
    "org_id", "org_name", "category", "org_description", "website_url", "is_active",
    "org_phone", "org_email", "street_address", "city", "state", "zip", "last_checked",
)


def source_item(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a source row into the small, flat scraper input format.

    Also accepts Markdown-table rows and older target files that kept the
    contact fields under "source_fields".
    """
    nested = record.get("source_fields") or {}
    item = {field: clean_text(nested.get(field, record.get(field))) for field in TARGET_FIELDS}
    item["org_name"] = clean_text(record.get("organization_name") or record.get("org_name"))
    item["website_url"] = markdown_url(record.get("source_url") or record.get("website_url"))
    return item


def load_items(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("organizations") or payload.get("targets") or payload.get("records")
        if not isinstance(payload, list):
            raise ValueError("The JSON input must be a list or contain organizations/targets/records.")
        items = [source_item(record) for record in payload]
    else:
        items = [source_item(record) for record in parse_markdown_table(path)]

    invalid = [item["org_id"] or item["org_name"] for item in items if not item["website_url"]]
    if invalid:
        raise ValueError(f"Missing a website URL for: {', '.join(invalid)}")
    return items


def phone_normalized(value: str) -> str | None:
    """Create an E.164-like US value when the number has 10 or 11 digits."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return None


def address_kind(value: str) -> str:
    return "mailing" if re.search(r"\bP\.?\s*O\.?\s*Box\b", value, re.IGNORECASE) else "physical"


def phone_kind(context: str) -> str:
    return "hotline" if re.search(r"24\s*(?:/|-|hour)|hotline|crisis|emergency", context, re.I) else "main"


def new_result(item: dict[str, Any], data_origin: str) -> dict[str, Any]:
    result = {
        "org_id": item["org_id"],
        "org_name": item["org_name"],
        "category": item["category"],
        "org_description": item["org_description"],
        "website_url": item["website_url"],
        "org_phone": item["org_phone"],
        "org_email": item["org_email"],
        "street_address": item["street_address"],
        "city": item["city"],
        "state": item["state"],
        "zip": item["zip"],
        "is_active": item["is_active"],
        "last_checked": item["last_checked"],
        "scraped_at": utc_now() if data_origin == "live_scrape" else None,
        "data_origin": data_origin,
        "contacts": {
            "addresses": [],
            "phones": [],
            "emails": [],
            "contact_form_url": None,
            "map_links": [],
        },
        "extraction": {
            "status": "pending",
            "pages_checked": [],
            "errors": [],
            "notes": [],
        },
    }

    initialize(result)
    return result


def unique_add(records: list[dict[str, Any]], candidate: dict[str, Any], key: str, count: bool = False) -> None:
    """Append a contact only once, while retaining the first source location.

    With ``count``, repeat sightings are tallied in ``_seen`` so values shown on
    every page (usually the footer) rank higher; tidy_contacts removes the tally.
    """
    comparison = clean_text(candidate[key]).lower()
    if not comparison:
        return
    for row in records:
        if clean_text(row[key]).lower() == comparison:
            if count:
                row["_seen"] = row.get("_seen", 1) + 1
            return
    records.append(candidate)


def split_phone_values(value: str) -> list[str]:
    matches = [clean_text(match.group(0)) for match in PHONE_RE.finditer(value)]
    if matches:
        return matches

    # Telephone links commonly contain an unformatted ten-digit value, such as
    # tel:8036427524.  Preserve it instead of discarding the public number.
    digits = re.sub(r"\D", "", clean_text(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return [f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"]
    return []


def source_address(item: dict[str, Any]) -> str | None:
    street = usable_value(item.get("street_address"))
    city = usable_value(item.get("city"))
    state = usable_value(item.get("state"))
    zip_code = usable_value(item.get("zip"))
    if not street:
        return None
    tail = " ".join(part for part in (state, zip_code) if part)
    locality = ", ".join(part for part in (city, tail) if part)
    return ", ".join(part for part in (street, locality) if part)


def build_inventory(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert existing table values to JSON without claiming they are a new crawl."""
    output: list[dict[str, Any]] = []
    for item in items:
        result = new_result(item, "provided_inventory")
        result["extraction"]["status"] = "source_inventory"
        result["extraction"]["notes"].append(
            "Converted from the supplied organization table; refresh with live scrape mode."
        )
        if item["org_id"] in PRIVATE_ADDRESS_ORG_IDS:
            result["extraction"]["notes"].append(
                "Address omitted because this organization has a confidential shelter location."
            )
        else:
            address = source_address(item)
            if address:
                unique_add(
                    result["contacts"]["addresses"],
                    {
                        "raw": address,
                        "kind": address_kind(address),
                        "source_url": None,
                        "method": "provided_inventory",
                    },
                    "raw",
                )

        primary = usable_value(item.get("org_phone"))
        if primary:
            for phone in split_phone_values(primary):
                unique_add(
                    result["contacts"]["phones"],
                    {
                        "raw": phone,
                        "normalized": phone_normalized(phone),
                        "source_url": None,
                        "method": "provided_inventory",
                    },
                    "raw",
                )

        email = usable_value(item.get("org_email"))
        if email and EMAIL_RE.fullmatch(email):
            unique_add(
                result["contacts"]["emails"],
                {
                    "value": email.lower(),
                    "source_url": None,
                    "method": "provided_inventory",
                },
                "value",
            )
        elif clean_text(item.get("org_email")).lower() == "contact form only":
            result["extraction"]["notes"].append("A contact form is documented; no public email was supplied.")

        finalize(result)
        output.append(result)
    return output


def normalized_host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def same_domain(first: str, second: str) -> bool:
    return normalized_host(first) == normalized_host(second)


def clean_address(value: Any) -> str:
    cleaned = clean_text(value)
    cleaned = re.sub(
        r"^(?:mailing address|physical address|address|mailing|location|visit us|our office)\s*:?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" |;")


def looks_like_address(value: str) -> bool:
    if not value:
        return False
    return bool(
        re.search(r"\bP\.?\s*O\.?\s*Box\s+\d+", value, re.I)
        or ADDRESS_RE.search(value)
    )


def json_ld_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from json_ld_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from json_ld_objects(nested)


JSON_LD_ORGANIZATION_TYPES = {
    "organization",
    "localbusiness",
    "nonprofitorganization",
    "governmentorganization",
    "ngo",
    "charity",
    "civicstructure",
}


def json_ld_type_names(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {clean_text(entry).lower() for entry in values if clean_text(entry)}


def json_ld_matches_target(obj: dict[str, Any], organization_name: str) -> bool:
    """Only trust JSON-LD that identifies the organization being scraped."""
    if not (json_ld_type_names(obj.get("@type")) & JSON_LD_ORGANIZATION_TYPES):
        return False
    candidate = re.sub(r"[^a-z0-9]+", " ", clean_text(obj.get("name")).lower()).strip()
    target = re.sub(r"[^a-z0-9]+", " ", clean_text(organization_name).lower()).strip()
    if not candidate or not target:
        return False
    if len(candidate) >= 6 and (candidate in target or target in candidate):
        return True
    candidate_words = {word for word in candidate.split() if len(word) >= 4}
    target_words = {word for word in target.split() if len(word) >= 4}
    return len(candidate_words & target_words) >= 2


def format_json_ld_address(value: Any) -> str | None:
    if isinstance(value, str):
        return clean_address(value) if looks_like_address(value) else None
    if not isinstance(value, dict):
        return None
    street = clean_address(value.get("streetAddress"))
    city = clean_text(value.get("addressLocality"))
    region = clean_text(value.get("addressRegion"))
    postal = clean_text(value.get("postalCode"))
    locality = ", ".join(part for part in (city, " ".join(part for part in (region, postal) if part)) if part)
    joined = ", ".join(part for part in (street, locality) if part)
    return joined if looks_like_address(joined) else None


def add_address(result: dict[str, Any], value: Any, page_url: str, method: str) -> None:
    if result["org_id"] in PRIVATE_ADDRESS_ORG_IDS:
        return
    address = clean_address(value)
    if looks_like_address(address):
        unique_add(
            result["contacts"]["addresses"],
            {
                "raw": address,
                "kind": address_kind(address),
                "source_url": page_url,
                "method": method,
            },
            "raw",
            count=True,
        )


def add_phone(
    result: dict[str, Any], value: Any, page_url: str, method: str, context: str = ""
) -> None:
    hotline = phone_kind(context) == "hotline"
    for phone in split_phone_values(clean_text(value)):
        phones = result["contacts"]["phones"]
        unique_add(
            phones,
            {
                "raw": phone,
                "normalized": phone_normalized(phone),
                "labeled_hotline": hotline,
                "source_url": page_url,
                "method": method,
            },
            "raw",
            count=True,
        )
        if hotline:
            for row in phones:
                if row["raw"].lower() == phone.lower():
                    row["labeled_hotline"] = True


def add_email(result: dict[str, Any], value: Any, page_url: str, method: str) -> None:
    for email in EMAIL_RE.findall(clean_text(value)):
        unique_add(
            result["contacts"]["emails"],
            {
                "value": email.lower(),
                "source_url": page_url,
                "method": method,
            },
            "value",
            count=True,
        )


def add_map_link(result: dict[str, Any], link: Any, page_url: str) -> None:
    """Keep a public map link and recover an address when the URL exposes one."""
    if result["org_id"] in PRIVATE_ADDRESS_ORG_IDS:
        return
    href = clean_text(link.get("href"))
    if not href:
        return
    unique_add(
        result["contacts"]["map_links"],
        {"url": href, "text": link_text(link), "source_url": page_url},
        "url",
        count=True,
    )

    values = [link_text(link)]
    parsed = urlparse(href)
    decoded_path = unquote(parsed.path).replace("+", " ")
    if "/maps/place/" in decoded_path:
        values.append(decoded_path.split("/maps/place/", 1)[1].split("/@", 1)[0])
    for key in ("q", "query", "destination", "daddr", "saddr"):
        values.extend(unquote(entry).replace("+", " ") for entry in parse_qs(parsed.query).get(key, []))
    for value in values:
        add_address(result, value, page_url, "map_link")


# ---------------------------------------------------------------------------
# Contact cleanup: validate, merge duplicates, rank, and keep the best few.
# ---------------------------------------------------------------------------

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
# A complete address: street (or PO Box), optional unit, city, state, ZIP.
FULL_ADDRESS_RE = re.compile(
    r"(?P<street>P\.?\s*O\.?\s*Box\s+\d+|\d{1,6}\s+(?:(?!\d+\s)[A-Za-z0-9#.'’&-]+\s+){0,6}?"
    rf"(?:{STREET_SUFFIXES})(?![A-Za-z])(?:\s+(?-i:(?:[NS]\.?[EW]|[NSEW]))\.?(?![A-Za-z]))?)"
    r"(?P<unit>\s*,?\s*(?:(?:Suite|Ste|Unit|Building|Bldg|Floor|Fl|Room|Rm)\.?(?![A-Za-z])|#)\s*[A-Za-z0-9-]+)?"
    r"\s*,?\s*(?P<city>[A-Za-z][A-Za-z.'’ -]{0,30}?)\s*,?\s*(?P<state>(?-i:[A-Z]\.?[A-Z]))\.?,?\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)(?!\d)",
    re.IGNORECASE,
)
# Words that show a "street" is really page text, e.g. "1715 Clerk of Court".
ADDRESS_JUNK_RE = re.compile(
    r"\b(?:chat|text|call|email|e-mail|phone|fax|clerk|division|department|hours|open|closed|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|copyright|directions|learn|click|map)\b",
    re.IGNORECASE,
)
# How much each way of finding a value is trusted when ranking.
METHOD_SCORES = {
    "json_ld": 5, "tel_link": 4, "mailto_link": 4, "semantic_address": 3,
    "map_link": 3, "contact_container_text": 2,
}
# Staff, board, and directory pages list individual people's contacts.
LISTING_PAGE_RE = re.compile(
    r"\b(?:staff|directory|employees?|board|team|leadership|careers?|jobs?|news|blog|events?|calendar)\b",
    re.IGNORECASE,
)
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com", "icloud.com", "msn.com",
    "live.com", "comcast.net", "att.net", "bellsouth.net", "mail.com", "protonmail.com", "ymail.com",
}
PLACEHOLDER_EMAIL_DOMAINS = ("example.com", "example.org", "domain.com", "yourdomain.com", "email.com",
                             "sentry.io", "wixpress.com")
ROLE_EMAIL_RE = re.compile(
    r"^(?:info|contact|hello|office|admin|help|support|general|inquiries|questions|hope|services|"
    r"reception|frontdesk|main|mail)\b",
    re.IGNORECASE,
)
FILE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}


def is_listing_page(url: str, label: str = "") -> bool:
    path = urlparse(url).path
    if "contact" in path.lower():
        return False
    return bool(LISTING_PAGE_RE.search(f"{re.sub(r'[^A-Za-z]+', ' ', path)} {label}"))


def full_addresses(value: Any) -> list[str]:
    """Every complete, plausible address inside a piece of text, consistently formatted."""
    found = []
    for match in FULL_ADDRESS_RE.finditer(clean_address(value)):
        street = clean_text(match["street"]).strip(" ,")
        city = clean_text(match["city"]).strip(" ,.")
        state = match["state"].replace(".", "")
        if state not in US_STATES or not city or len(city.split()) > 3:
            continue
        if ADDRESS_JUNK_RE.search(f"{street} {city}"):
            continue
        unit = clean_text(match["unit"] or "").strip(" ,")
        found.append(", ".join([street, *([unit] if unit else []), city]) + f", {state} {match['zip']}")
    return found


def address_key(address: str) -> tuple[str, str]:
    """Same house number (or PO Box) and ZIP means the same place."""
    box = re.match(r"P\.?\s*O\.?\s*Box\s+(\d+)", address, re.I)
    number = f"po{box[1]}" if box else re.match(r"\d+", address)[0]
    return number, re.search(r"(\d{5})(?:-\d{4})?$", address)[1]


def phone_key(value: str) -> tuple[str, str] | None:
    """(ten digits, extension) for a valid US number, else None."""
    extension = re.search(r"(?:x|ext\.?|extension)\s*(\d{1,6})\s*$", value, re.I)
    digits = re.sub(r"\D", "", value[: extension.start()] if extension else value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # Area codes and exchanges never start with 0 or 1.
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return digits, extension[1] if extension else ""


def format_phone(key: tuple[str, str]) -> str:
    digits, extension = key
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" + (f" ext. {extension}" if extension else "")


def org_words(result: dict[str, Any]) -> set[str]:
    name = result["original_fields"]["org_name"]
    label = normalized_host(result["original_fields"]["website_url"]).split(".")[0]
    return {word for word in re.findall(r"[a-z]{4,}", f"{name} {label}".lower().replace("-", " "))}


def email_rejected(result: dict[str, Any], email: str) -> bool:
    local, _, domain = email.partition("@")
    site = normalized_host(result["original_fields"]["website_url"])
    if domain.rsplit(".", 1)[-1] in FILE_EXTENSIONS or domain.endswith(PLACEHOLDER_EMAIL_DOMAINS):
        return True
    if domain != site and site.startswith(domain):
        return True  # cut off mid-domain, e.g. "...@childrensplaceinc.or"
    if domain in FREE_MAIL_DOMAINS:
        # Keep "defendersforchildren@gmail.com"; drop individuals' personal mail.
        compact = re.sub(r"[^a-z]", "", local)
        return not any(word in compact for word in org_words(result))
    return False


def provided_phone_keys(value: str) -> set[str]:
    return {key[0] for key in map(phone_key, split_phone_values(clean_text(value))) if key}


def provided_addresses(result: dict[str, Any]) -> list[str]:
    original = result["original_fields"]
    street = re.sub(r"\([^)]*\)", "", original["street_address"])  # "(from IRS record)" notes
    if not usable_value(street):
        return []
    return full_addresses(f"{street}, {original['city']}, {original['state']} {original['zip']}")


# --- Grading -------------------------------------------------------------
#
# Every valid phone, address, and email gets points, a confidence label, and
# the reasons for its score.  The main signal is how often the value appears
# across the pages checked: an organization's own number or address is
# usually repeated on most pages (header/footer/contact page), while a
# partner's or program's appears once.

POINTS = {
    "per_page": 2,          # shown on a checked page (counted for up to 5 pages)
    "extra_mention": 1,     # each extra mention on those pages (up to 3)
    "client_match": 3,      # matches the client's existing list
    "strong_source": 2,     # phone/email link or the site's structured data
    "section_source": 1,    # address/contact section, map link, or link text
    "key_page": 1,          # on the home page or a contact page
    "same_state": 1,
    "other_state": -3,
    "own_domain": 2,
    "general_inbox": 1,
    "special_inbox": -2,
    "hidden_link": -3,      # link dials a number it does not show
    "national_hotline": -6,
}
HIGH_CONFIDENCE, MEDIUM_CONFIDENCE = 9, 5
STRONG_METHODS = {"tel_link", "tel_link_text", "mailto_link", "json_ld"}
SECTION_METHODS = {"semantic_address", "map_link", "contact_container_text"}
SPECIAL_EMAIL_RE = re.compile(
    r"web ?(?:master|manager|admin)|privacy|no-?reply|donotreply|volunteer|donat|development|giving|"
    r"career|jobs|press|media|marketing|events|billing|newsletter",
    re.IGNORECASE,
)
# National lines that many charity sites list but that are not the charity's own.
NATIONAL_HOTLINES = {
    "8007997233": "National Domestic Violence Hotline",
    "8006564673": "RAINN Sexual Assault Hotline",
    "8883737888": "National Human Trafficking Hotline",
    "8002738255": "National Suicide Prevention Lifeline",
    "8004224453": "Childhelp National Child Abuse Hotline",
    "8663319474": "love is respect",
    "8006624357": "SAMHSA National Helpline",
    "8009506264": "NAMI HelpLine",
}


def visible_text(soup: Any) -> str:
    """Page text a visitor sees (no scripts, styles, or comments)."""
    skip = {"script", "style", "noscript", "template"}
    return clean_text(" ".join(
        node for node in soup.find_all(string=True)
        if node.parent.name not in skip and type(node).__name__ == "NavigableString"
    ))


def record_page(result: dict[str, Any], soup: Any, page_url: str) -> None:
    """Keep what grading needs from a page; removed before the record is saved."""
    result.setdefault("_pages", {})[page_url] = {
        "text": visible_text(soup),
        "tel": [key[0] for key in (phone_key(unquote(a.get("href", "")[4:]).split("?", 1)[0])
                                   for a in soup.select('a[href^="tel:"]')) if key],
        "mailto": [unquote(a.get("href", "")[7:]).split("?", 1)[0].strip().lower()
                   for a in soup.select('a[href^="mailto:"]')],
    }


def phone_pattern(digits: str) -> re.Pattern:
    a, e, n = digits[:3], digits[3:6], digits[6:]
    return re.compile(rf"(?<!\d)(?:\+?1[\s.\-]*)?\(?{a}\)?[\s.\-]*{e}[\s.\-]*{n}(?!\d)")


def address_pattern(address: str) -> re.Pattern | None:
    """Street number + street name (or PO Box number), however it is written."""
    box = re.match(r"P\.?\s*O\.?\s*Box\s+(\d+)", address, re.I)
    if box:
        return re.compile(rf"P\.?\s*O\.?\s*Box\s+{box[1]}\b", re.I)
    match = re.match(r"(\d+)\s+(?:(?:N|S|E|W|North|South|East|West)\.?\s+)?([A-Za-z0-9']+)", address)
    if not match:
        return None
    return re.compile(
        rf"\b{match[1]}\s+(?:(?:N|S|E|W|North|South|East|West)\.?\s+)?{re.escape(match[2])}\b", re.I)


def confirms_address(text: str, address: str) -> bool:
    pattern = address_pattern(address)
    return bool(pattern and pattern.search(text))


def is_key_page(result: dict[str, Any], url: str) -> bool:
    pages = list(result.get("_pages", {}))
    return bool(pages and url == pages[0]) or bool(
        re.search(r"contact|location|office|visit|directions", urlparse(url).path, re.I))


def page_presence(result: dict[str, Any], count_text, in_links=lambda page: False) -> tuple[list[str], int]:
    """(pages the value appears on, total visible mentions)."""
    pages, mentions = [], 0
    for url, page in result.get("_pages", {}).items():
        found = count_text(page["text"])
        mentions += found
        if found or in_links(page):
            pages.append(url)
    return pages, mentions


def grade(result: dict[str, Any], entry: dict[str, Any], pages: list[str], mentions: int,
          methods: set[str], extra: list[tuple[int, str]]) -> dict[str, Any]:
    """Attach score, confidence, and reasons to a contact entry."""
    total_pages = len(result.get("_pages", {})) or 1
    reasons: list[tuple[int, str]] = []
    if pages:
        reasons.append((POINTS["per_page"] * min(len(pages), 5), f"shown on {len(pages)} of {total_pages} pages checked"))
        if mentions > len(pages):
            reasons.append((POINTS["extra_mention"] * min(mentions - len(pages), 3),
                            f"mentioned {mentions} times in all"))
        if any(is_key_page(result, url) for url in pages):
            reasons.append((POINTS["key_page"], "on the home page or a contact page"))
    if methods & STRONG_METHODS:
        label = "site's structured data" if methods & STRONG_METHODS == {"json_ld"} else "clickable phone/email link"
        reasons.append((POINTS["strong_source"], label))
    elif methods & SECTION_METHODS or any(m.endswith("_confirms_provided") for m in methods):
        reasons.append((POINTS["section_source"], "in an address/contact section or map link"))
    reasons.extend(extra)
    score = sum(points for points, _ in reasons)
    confidence = "high" if score >= HIGH_CONFIDENCE else "medium" if score >= MEDIUM_CONFIDENCE else "low"
    return {**entry, "score": score, "confidence": confidence, "primary": False,
            "reasons": [f"{points:+d} {why}" if points else why for points, why in reasons]}


def best_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    order = ["json_ld", "tel_link", "mailto_link", "tel_link_text", "semantic_address", "map_link"]
    return min(rows, key=lambda row: order.index(row["method"]) if row["method"] in order else len(order))


def grade_addresses(result: dict[str, Any]) -> list[dict[str, Any]]:
    provided_list = provided_addresses(result)
    provided = {address_key(a) for a in provided_list}
    client_state = clean_text(result["original_fields"]["state"]).upper()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in result["contacts"]["addresses"]:
        for address in full_addresses(row["raw"]):
            group = groups.setdefault(address_key(address), {"rows": [], "best": None})
            group["rows"].append(row)
            # Keep the most detailed wording of the same place.
            if group["best"] is None or len(address) > len(group["best"]):
                group["best"] = address
    # Fix #1: the client's address counts when its street appears anywhere on
    # a checked page, even outside the contact sections scanned above.
    for address in provided_list:
        key = address_key(address)
        if key in groups:
            continue
        pages = [url for url, page in result.get("_pages", {}).items() if confirms_address(page["text"], address)]
        rows = [row for row in result["contacts"]["addresses"] if confirms_address(row["raw"], address)]
        if pages or rows:
            source = rows[0]["source_url"] if rows else pages[0]
            groups[key] = {"rows": rows or [{"source_url": source, "method": "confirmed_on_page"}], "best": address}
            groups[key]["rows"][0] = {**groups[key]["rows"][0], "method": (
                f"{rows[0]['method']}_confirms_provided" if rows else "confirmed_on_page")}

    graded = []
    for key, group in groups.items():
        address = group["best"]
        pattern = address_pattern(address)
        pages, mentions = page_presence(result, lambda text: len(pattern.findall(text)) if pattern else 0)
        extra = []
        if key in provided:
            extra.append((POINTS["client_match"], "matches the client's list"))
        state = re.search(r",\s*([A-Z]{2})\s+\d{5}", address)
        if client_state in US_STATES and state:
            if state[1] == client_state:
                extra.append((POINTS["same_state"], f"in the client's state ({client_state})"))
            else:
                extra.append((POINTS["other_state"], f"outside the client's state ({state[1]})"))
        row = best_row(group["rows"])
        entry = {"raw": address, "kind": address_kind(address),
                 "source_url": row["source_url"], "method": row["method"]}
        graded.append(grade(result, entry, pages, mentions, {r["method"] for r in group["rows"]}, extra))
    graded.sort(key=lambda entry: -entry["score"])
    # A street address people can visit beats a PO Box unless it is a weak match.
    primary = next((e for e in graded if e["kind"] == "physical" and e["confidence"] != "low"), None)
    if graded:
        (primary or graded[0])["primary"] = True
    return graded


def grade_phones(result: dict[str, Any]) -> list[dict[str, Any]]:
    provided = provided_phone_keys(result["original_fields"]["org_phone"])
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in result["contacts"]["phones"]:
        key = phone_key(row["raw"])
        if key:
            group = groups.setdefault(key, {"rows": [], "hotline": False})
            group["rows"].append(row)
            group["hotline"] |= bool(row.get("labeled_hotline"))
    # Fix #1: the client's number counts when it appears anywhere on a page.
    for digits in provided:
        key = (digits, "")
        if key not in groups:
            pages = [url for url, page in result.get("_pages", {}).items() if phone_pattern(digits).search(page["text"])]
            if pages:
                groups[key] = {"rows": [{"source_url": pages[0], "method": "confirmed_on_page"}], "hotline": False}

    graded = []
    for key, group in groups.items():
        digits = key[0]
        pattern = phone_pattern(digits)
        pages, mentions = page_presence(result, lambda text: len(pattern.findall(text)),
                                        lambda page: digits in page["tel"])
        methods = {row["method"] for row in group["rows"]}
        extra = []
        if digits in provided:
            extra.append((POINTS["client_match"], "matches the client's list"))
        elif digits in NATIONAL_HOTLINES:
            extra.append((POINTS["national_hotline"], f"{NATIONAL_HOTLINES[digits]}, not this organization's own line"))
        if methods == {"tel_link_hidden"}:
            extra.append((POINTS["hidden_link"], "only inside a phone link that shows a different number"))
        if group["hotline"]:
            extra.append((0, "labeled on the site as a hotline/24-7/crisis line"))
        row = best_row(group["rows"])
        entry = {"raw": format_phone(key), "normalized": f"+1{digits}", "labeled_hotline": group["hotline"],
                 "source_url": row["source_url"], "method": row["method"]}
        graded.append(grade(result, entry, pages, mentions, methods - {"tel_link_hidden"}, extra))
    graded.sort(key=lambda entry: -entry["score"])
    if graded:
        graded[0]["primary"] = True
    return graded


def grade_emails(result: dict[str, Any]) -> list[dict[str, Any]]:
    label = normalized_host(result["original_fields"]["website_url"]).split(".")[0]
    provided = clean_text(result["original_fields"]["org_email"]).lower()
    rows_by_email: dict[str, list[dict[str, Any]]] = {}
    for row in result["contacts"]["emails"]:
        if not email_rejected(result, row["value"]):
            rows_by_email.setdefault(row["value"], []).append(row)
    # Fix #1: the client's email counts when it appears anywhere on a page.
    if EMAIL_RE.fullmatch(provided or "-") and provided not in rows_by_email:
        pages = [url for url, page in result.get("_pages", {}).items()
                 if provided in page["text"].lower() or provided in page["mailto"]]
        if pages:
            rows_by_email[provided] = [{"source_url": pages[0], "method": "confirmed_on_page"}]

    graded = []
    for email, rows in rows_by_email.items():
        local, _, domain = email.partition("@")
        pages, mentions = page_presence(result, lambda text: text.lower().count(email),
                                        lambda page: email in page["mailto"])
        extra = []
        if email == provided:
            extra.append((POINTS["client_match"], "matches the client's list"))
        if label in domain.split("."):
            extra.append((POINTS["own_domain"], "on the organization's own website domain"))
        if SPECIAL_EMAIL_RE.search(local):
            extra.append((POINTS["special_inbox"], "special-purpose inbox (web admin, volunteer, donations, ...)"))
        elif ROLE_EMAIL_RE.match(local):
            extra.append((POINTS["general_inbox"], "general inbox"))
        row = best_row(rows)
        entry = {"value": email, "source_url": row["source_url"], "method": row["method"]}
        graded.append(grade(result, entry, pages, mentions, {r["method"] for r in rows}, extra))
    graded.sort(key=lambda entry: -entry["score"])
    if graded:
        graded[0]["primary"] = True
    return graded


def contact_review_reasons(result: dict[str, Any]) -> list[str]:
    """Plain-language reasons a person should look at the primary contacts."""
    original, contacts, reasons = result["original_fields"], result["contacts"], []
    checks = [
        ("phone", contacts["phones"], "raw", provided_phone_keys(original["org_phone"]),
         lambda entry: entry["normalized"][2:]),
        ("email", contacts["emails"], "value", {clean_text(original["org_email"]).lower()} - {""},
         lambda entry: entry["value"]),
        ("address", contacts["addresses"], "raw", {address_key(a) for a in provided_addresses(result)},
         lambda entry: address_key(entry["raw"])),
    ]
    for label, entries, key, client_values, compare in checks:
        primary = next((entry for entry in entries if entry.get("primary")), None)
        if not primary:
            continue
        if primary["confidence"] == "low":
            reasons.append(f"Primary {label} {primary[key]} has low confidence (score {primary['score']}).")
        if client_values and compare(primary) not in client_values:
            reasons.append(f"Primary {label} {primary[key]} differs from the client's list.")
    return reasons


def keep_best(entries: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """The top ``limit`` entries by score, always including the primary one."""
    kept = entries[:limit]
    primary = next((entry for entry in entries if entry["primary"]), None)
    if primary and primary not in kept:
        kept = kept[:-1] + [primary]  # e.g. a street address chosen over higher-scoring PO Boxes
    return kept


def tidy_contacts(result: dict[str, Any], limit: int | None) -> None:
    """Replace raw contact lists with validated, merged, graded ones."""
    contacts = result["contacts"]
    contacts["addresses"] = keep_best(grade_addresses(result), limit)
    contacts["phones"] = keep_best(grade_phones(result), limit)
    contacts["emails"] = keep_best(grade_emails(result), limit)
    contacts["map_links"] = [{k: v for k, v in row.items() if k != "_seen"}
                             for row in contacts["map_links"]][:limit]
    result["extraction"]["contact_review"] = contact_review_reasons(result)
    result.pop("_pages", None)


def nearby_text(anchor: Any) -> str:
    """The link's own block plus the text just before it, e.g. "Call our 24/7 line:"."""
    before = []
    for piece in anchor.find_all_previous(string=True, limit=12):
        if piece.parent.name in ("script", "style"):
            continue
        before.append(clean_text(piece))
        if sum(map(len, before)) > 100:
            break
    return clean_text(f"{' '.join(reversed(before))} {link_text(anchor.parent)}")


def link_text(link: Any) -> str:
    try:
        return clean_text(link.get_text(" ", strip=True))
    except Exception:
        return ""


def find_contact_pages(soup: Any, page_url: str, root_url: str) -> list[str]:
    """Return at most two likely contact/location pages on the same site."""
    candidates: list[tuple[int, str]] = []
    for link in soup.select("a[href]"):
        href = clean_text(link.get("href"))
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        if not absolute.startswith(("http://", "https://")) or not same_domain(root_url, absolute):
            continue
        if is_listing_page(absolute, link_text(link)):
            continue
        haystack = f"{absolute.lower()} {link_text(link).lower()}"
        matches = sum(keyword in haystack for keyword in CONTACT_KEYWORDS)
        if matches:
            score = matches + (4 if "contact" in haystack else 0) + (2 if "location" in haystack else 0)
            candidates.append((score, absolute.split("#", 1)[0]))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    selected: list[str] = []
    for _, candidate in candidates:
        if candidate not in selected and candidate != page_url:
            selected.append(candidate)
        if len(selected) == 2:
            break
    return selected


def has_contact_form(soup: Any, page_url: str) -> bool:
    for form in soup.select("form"):
        form_text = clean_text(form.get_text(" ", strip=True))
        field_names = " ".join(clean_text(tag.get("name")) for tag in form.select("input, textarea"))
        if "contact" in page_url.lower() or re.search(r"message|inquiry|question", f"{form_text} {field_names}", re.I):
            return True
    return False


def extract_from_page(result: dict[str, Any], soup: Any, page_url: str) -> list[str]:
    """Extract contacts from links, JSON-LD, semantic address blocks, and fallback text."""
    extract(result, soup, page_url)
    for anchor in soup.select('a[href^="tel:"]'):
        href = clean_text(anchor.get("href"))
        value = unquote(href[4:]).split("?", 1)[0]
        context = nearby_text(anchor)
        dialed = phone_key(value)
        shown = [match.group(0) for match in PHONE_RE.finditer(link_text(anchor))]
        if dialed and shown and dialed[0] not in {key[0] for key in map(phone_key, shown) if key}:
            # The link shows one number but dials another (an outdated link).
            # Visitors read the shown number, so it is graded normally and the
            # dialed one is penalized.
            for number in shown:
                add_phone(result, number, page_url, "tel_link_text", context)
            add_phone(result, value, page_url, "tel_link_hidden", context)
            result["extraction"]["notes"].append(
                f"{page_url}: a phone link shows {', '.join(shown)} but dials {format_phone(dialed)}.")
        else:
            add_phone(result, value, page_url, "tel_link", context)

    for anchor in soup.select('a[href^="mailto:"]'):
        href = clean_text(anchor.get("href"))
        value = unquote(href[7:]).split("?", 1)[0]
        add_email(result, value, page_url, "mailto_link")

    for script in soup.select('script[type="application/ld+json"]'):
        raw_json = script.string or script.get_text()
        try:
            data = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            continue
        for obj in json_ld_objects(data):
            if not json_ld_matches_target(obj, result["original_fields"]["org_name"]):
                continue
            structured(result, obj, page_url)
            for phone in (obj.get("telephone"), obj.get("phone")):
                if isinstance(phone, list):
                    for value in phone:
                        add_phone(result, value, page_url, "json_ld")
                else:
                    add_phone(result, phone, page_url, "json_ld")
            for email in (obj.get("email"),):
                if isinstance(email, list):
                    for value in email:
                        add_email(result, value, page_url, "json_ld")
                else:
                    add_email(result, email, page_url, "json_ld")
            address = format_json_ld_address(obj.get("address"))
            if address:
                add_address(result, address, page_url, "json_ld")

    address_selectors = (
        "address, [itemprop='address'], [itemprop='streetAddress'], "
        ".address, .addresses, .contact-address, .location, .locations, "
        ".office-address, .footer-address"
    )
    for element in soup.select(address_selectors):
        add_address(result, element.get_text(" ", strip=True), page_url, "semantic_address")

    for anchor in soup.select(
        'a[href*="google.com/maps"], a[href*="maps.google"], a[href*="goo.gl/maps"], a[href*="maps.apple.com"]'
    ):
        add_map_link(result, anchor, page_url)

    # A whole-page text scan can accidentally take a vendor or partner's
    # details. Restrict fallbacks to contact/location/footer containers.
    fallback_containers = (
        "footer, address, [class*='contact'], [id*='contact'], "
        "[class*='location'], [id*='location'], [class*='address'], [id*='address']"
    )
    for element in soup.select(fallback_containers):
        container_text = clean_text(element.get_text(" ", strip=True))
        for match in ADDRESS_RE.finditer(container_text):
            add_address(result, match.group(0), page_url, "contact_container_text")
        for match in PHONE_RE.finditer(container_text):
            # Labels such as "24/7 crisis line:" come just before the number.
            nearby = container_text[max(0, match.start() - 60) : match.end()]
            add_phone(result, match.group(0), page_url, "contact_container_text", nearby)
        for email in EMAIL_RE.findall(container_text):
            add_email(result, email, page_url, "contact_container_text")

    if has_contact_form(soup, page_url) and not result["contacts"]["contact_form_url"]:
        result["contacts"]["contact_form_url"] = page_url
    return find_contact_pages(soup, page_url, result["website_url"])


def set_result_status(result: dict[str, Any]) -> None:
    contacts = result["contacts"]
    core_fields_found = sum(
        (
            bool(contacts["addresses"]),
            bool(contacts["phones"]),
            bool(contacts["emails"]),
        )
    )
    errors = result["extraction"]["errors"]
    if not result["extraction"]["pages_checked"]:
        result["extraction"]["status"] = "failed"
    elif core_fields_found == 0:
        result["extraction"]["status"] = (
            "manual_review" if result["org_id"] in KNOWN_MANUAL_REVIEW_IDS else "not_found"
        )
    elif core_fields_found < 3 or errors:
        result["extraction"]["status"] = "partial"
    else:
        result["extraction"]["status"] = "success"

    if result["org_id"] in PRIVATE_ADDRESS_ORG_IDS:
        result["extraction"]["notes"].append(
            "Address extraction was omitted to protect a confidential shelter location."
        )


class NeedsBrowser(Exception):
    """Plain HTTP could not load the site the way a browser would."""


class BrowserGone(Exception):
    """Chrome crashed or disconnected; the site is retried in a fresh browser."""


BROWSER_GONE_RE = re.compile(
    r"connection refused|errno 111|connection (?:reset|closed|lost)|target closed|browser (?:has )?disconnected",
    re.IGNORECASE,
)


# Titles of bot-check / access-denied interstitials.  In auto mode these send
# the site to the browser pass; nothing here tries to get past them.
INTERSTITIAL_TITLE_RE = re.compile(
    r"just a moment|attention required|checking your browser|access denied|are you a robot|captcha",
    re.IGNORECASE,
)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Titles of error, outage, and access-check pages.  Seeing one on a site's
# start page marks the site as failed instead of "no contacts found".
ERROR_TITLE_RE = re.compile(
    r"page not found|\bnot found\b|internal server error|bad gateway|service unavailable|gateway time-?out|"
    r"database error|›\s*error|^\s*error\b|site (?:is )?(?:temporarily )?(?:down|unavailable|offline)|"
    r"account (?:has been )?suspended|too many requests|forbidden|under construction|coming soon|"
    r"briefly unavailable|maintenance mode|just a moment|attention required|access denied|"
    r"checking your browser|are you a robot|captcha",
    re.IGNORECASE,
)


def error_page_reason(soup: Any) -> str | None:
    """Describe why a loaded page is an error/blocked/empty page, or return None."""
    title = clean_text(soup.title.get_text()) if soup.title else ""
    if ERROR_TITLE_RE.search(title):
        return f'an error or access check ("{title[:80]}")'
    body = soup.body or soup
    if len(clean_text(body.get_text(" ", strip=True))) < 30 and not soup.select_one("a[href]"):
        return "an empty page"
    return None


def looks_unrendered(soup: Any) -> bool:
    """True for interstitials and JavaScript shells that need a real browser."""
    title = clean_text(soup.title.get_text()) if soup.title else ""
    if INTERSTITIAL_TITLE_RE.search(title):
        return True
    body = soup.body or soup
    visible = " ".join(
        node for node in body.find_all(string=True)
        if node.parent.name not in ("script", "style", "noscript", "template")
    )
    return len(clean_text(visible)) < 200 and soup.find("script") is not None


def browser_fetcher(driver: Any, soupify_func: Any, delay_seconds: float, page_timeout: float) -> Any:
    """Load pages in Chrome.  The pause after each load lets scripts render."""
    import inspect

    get_kwargs = {"timeout": page_timeout} if "timeout" in inspect.signature(driver.get).parameters else {}

    def fetch(page_url: str) -> tuple[str, Any]:
        try:
            driver.get(page_url, **get_kwargs)
        except Exception as exc:
            # Without this, one crashed Chrome fails every remaining site.
            gone = bool(BROWSER_GONE_RE.search(str(exc)))
            if not gone and isinstance(exc, TimeoutError):
                # A crash during a page load surfaces as a timeout; check
                # whether Chrome is still there before blaming the site.
                try:
                    driver.run_js("return 1")
                except Exception as probe:
                    gone = bool(BROWSER_GONE_RE.search(str(probe)))
            if gone:
                raise BrowserGone(str(exc)) from exc
            raise
        if delay_seconds:
            time.sleep(delay_seconds)
        return getattr(driver, "current_url", ""), lambda: soupify_func(driver)

    return fetch


def http_fetcher(session: Any, delay_seconds: float, page_timeout: float) -> Any:
    """Load pages with plain HTTP, pausing between pages of the same site."""
    from bs4 import BeautifulSoup

    first_page = True

    def fetch(page_url: str) -> tuple[str, Any]:
        nonlocal first_page
        if delay_seconds and not first_page:
            time.sleep(delay_seconds)
        first_page = False
        response = session.get(page_url, timeout=page_timeout)
        if response.status_code in (401, 403, 429, 503):
            raise NeedsBrowser(f"HTTP {response.status_code} for {page_url}")
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            raise ValueError(f"Non-HTML response ({content_type or 'no content type'})")
        charset = re.search(r"charset=([\w-]+)", content_type, re.I)

        def load_soup() -> Any:
            soup = BeautifulSoup(response.content, "html.parser",
                                 from_encoding=charset[1] if charset else None)
            if looks_unrendered(soup):
                raise NeedsBrowser(f"{page_url} needs JavaScript or shows an access check")
            return soup

        return response.url, load_soup

    return fetch


def scrape_one_site(fetch: Any, item: dict[str, Any]) -> dict[str, Any]:
    result = new_result(item, "live_scrape")
    pages_to_visit = [item["website_url"]]
    profile_queue = []
    visited = set()
    profile_visits = 0
    contact_visits = 0
    max_profile_pages = int(item.get("max_profile_pages", 2))
    max_contact_pages = int(item.get("max_contact_pages", 2))

    while pages_to_visit or profile_queue:
        if pages_to_visit and contact_visits < max_contact_pages + 1:
            page_url = pages_to_visit.pop(0)
            queue_kind = "contact"
        elif profile_queue and profile_visits < max_profile_pages:
            page_url = profile_queue.pop(0)
            queue_kind = "profile"
        else:
            break
        if page_url in visited:
            continue
        visited.add(page_url)
        if queue_kind == "contact":
            contact_visits += 1
        else:
            profile_visits += 1
        try:
            actual_url, load_soup = fetch(page_url)
            actual_url = clean_text(actual_url)
            if actual_url.startswith(("http://", "https://")):
                if not same_domain(result["website_url"], actual_url):
                    result["extraction"]["errors"].append(
                        {
                            "source_url": page_url,
                            "message": f"Redirected outside the organization domain to {actual_url}; skipped.",
                        }
                    )
                    continue
                page_url = actual_url
            soup = load_soup()
            problem = error_page_reason(soup)
            if problem:
                message = f"Page shows {problem}; skipped."
                if result["extraction"]["pages_checked"]:
                    # A broken inner link does not make the whole site a failure.
                    result["extraction"]["notes"].append(f"{page_url}: {message}")
                else:
                    result["extraction"]["errors"].append({"source_url": page_url, "message": message})
                continue
            result["extraction"]["pages_checked"].append(page_url)
            record_page(result, soup, page_url)
            candidates = extract_from_page(result, soup, page_url)
            for candidate in profile_pages(soup, page_url, item["website_url"]):
                if candidate not in visited and candidate not in profile_queue:
                    profile_queue.append(candidate)
            for candidate in candidates:
                if candidate not in pages_to_visit and candidate not in result["extraction"]["pages_checked"]:
                    pages_to_visit.append(candidate)
        except (NeedsBrowser, BrowserGone):
            raise
        except Exception as exc:  # Keep the rest of the 58-site batch running.
            result["extraction"]["errors"].append(
                {"source_url": page_url, "message": f"{type(exc).__name__}: {clean_text(exc)[:240]}"}
            )

    tidy_contacts(result, item.get("max_per_type") or None)
    set_result_status(result)
    finalize(result, max_values=3)  # profile candidates (descriptions etc.)
    return result


def error_result(item: dict[str, Any], message: str) -> dict[str, Any]:
    result = new_result(item, "live_scrape")
    result["extraction"]["status"] = "failed"
    result["extraction"]["errors"].append({"source_url": item["website_url"], "message": message})
    finalize(result)
    return result


class Progress:
    """Thread-safe one-line-per-site progress output."""

    def __init__(self, total: int) -> None:
        self.total, self.done, self.started = total, 0, time.monotonic()
        self.lock = threading.Lock()

    def report(self, result: dict[str, Any], label: str) -> None:
        with self.lock:
            self.done += 1
            print(
                f"[{self.done}/{self.total}] {result['org_id']} {result['extraction']['status']} "
                f"via {label} ({time.monotonic() - self.started:.0f}s elapsed)",
                flush=True,
            )


def run_browser_scrape(
    items: list[dict[str, Any]], workers: int, delay_seconds: float, page_timeout: float,
    headless: bool, chrome_path: str | None, progress: Progress,
) -> list[dict[str, Any]]:
    """Scrape sites in Chrome, ``workers`` browsers at a time."""
    try:
        from botasaurus.browser import Driver, browser
        from botasaurus.soupify import soupify
    except ImportError as exc:
        raise SystemExit(
            "Live scrape mode needs Botasaurus. Run: python -m pip install -r requirements.txt"
        ) from exc

    options = {"chrome_executable_path": chrome_path} if chrome_path else {}

    # Each site is still crawled one page at a time with the same pause, so a
    # single site never sees more traffic than before; only different sites
    # run side by side.
    @browser(
        parallel=workers if workers > 1 else None,
        reuse_driver=True,
        block_images=True,
        headless=headless,
        wait_for_complete_page_load=False,
        close_on_crash=True,
        max_retry=2,  # Only BrowserGone escapes scrape_one_site, so this retries crashes only.
        output=None,
        create_error_logs=False,
        **options,
    )
    def scrape_contact_task(driver: Driver, data: dict[str, Any]) -> dict[str, Any]:
        result = scrape_one_site(browser_fetcher(driver, soupify, delay_seconds, page_timeout), data)
        result["extraction"]["fetch_method"] = "browser"
        progress.report(result, "browser")
        return result

    raw_results = scrape_contact_task(items)
    try:
        scrape_contact_task.close()
    except Exception:
        pass

    if isinstance(raw_results, dict):
        raw_results = [raw_results]
    if not isinstance(raw_results, list):
        raw_results = []

    results: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        candidate = raw_results[index] if index < len(raw_results) else None
        if isinstance(candidate, dict) and candidate.get("org_id") == item["org_id"]:
            results.append(candidate)
        else:
            results.append(error_result(item, "Botasaurus did not return a result for this site."))
    return results


def run_http_scrape(
    items: list[dict[str, Any]], workers: int, delay_seconds: float, page_timeout: float,
    progress: Progress,
) -> list[dict[str, Any] | None]:
    """Scrape sites with plain HTTP.  ``None`` marks a site that needs a browser."""
    import requests

    def scrape(item: dict[str, Any]) -> dict[str, Any] | None:
        with requests.Session() as session:
            session.headers.update(HTTP_HEADERS)
            try:
                result = scrape_one_site(http_fetcher(session, delay_seconds, page_timeout), item)
            except NeedsBrowser:
                return None
        # A site with no contacts over HTTP may build them with JavaScript.
        if result["extraction"]["status"] in ("failed", "not_found", "manual_review"):
            return None
        result["extraction"]["fetch_method"] = "http"
        progress.report(result, "http")
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(scrape, items))


def run_live_scrape(
    items: list[dict[str, Any]], max_contact_pages: int, delay_seconds: float, headless: bool,
    max_profile_pages: int = 2, workers: int = 4, fetch_mode: str = "browser",
    http_workers: int = 16, page_timeout: float = 30.0, chrome_path: str | None = None,
    max_per_type: int = 5,
) -> list[dict[str, Any]]:
    """Run the live scrape.

    ``browser`` mode loads every page in Chrome.  ``auto`` mode tries plain
    HTTP first and sends only the sites that need JavaScript, block plain
    requests, or yield no contacts to Chrome.
    """
    batch = [
        {**item, "max_contact_pages": max_contact_pages, "max_profile_pages": max_profile_pages,
         "max_per_type": max_per_type}
        for item in items
    ]
    progress = Progress(len(batch))
    if fetch_mode == "browser":
        return run_browser_scrape(batch, workers, delay_seconds, page_timeout, headless, chrome_path, progress)

    results = run_http_scrape(batch, http_workers, delay_seconds, page_timeout, progress)
    retry = [index for index, result in enumerate(results) if result is None]
    if retry:
        print(f"{len(retry)} site(s) need a browser; opening Chrome for those.", flush=True)
        browser_results = run_browser_scrape(
            [batch[index] for index in retry], workers, delay_seconds, page_timeout,
            headless, chrome_path, progress,
        )
        for index, result in zip(retry, browser_results):
            results[index] = result
    return results


def write_json(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# Only these fields go into the organization import file, in this order.
ORGANIZATION_OUTPUT_FIELDS = (
    "org_id", "org_name", "category", "org_description", "website_url",
    "org_phone", "org_email", "street_address", "city", "state", "zip",
    "is_active", "last_checked",
)


def split_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate import fields from contacts/evidence without dropping any data.

    IDs and names are repeated in metadata so rows can be matched even when
    an input organization has a blank ID. Array order is also preserved.
    """
    organizations = []
    metadata = []
    for record in records:
        organizations.append({field: record.get(field, "") for field in ORGANIZATION_OUTPUT_FIELDS})
        metadata.append({
            "org_id": record.get("org_id", ""),
            "org_name": record.get("org_name", ""),
            **{key: value for key, value in record.items() if key not in ORGANIZATION_OUTPUT_FIELDS},
        })
    return organizations, metadata


def onedrive_destination(explicit: str | None) -> Path:
    """Use an explicit sync folder, or locate the user's local OneDrive root."""
    if explicit:
        return Path(explicit).expanduser()
    for key in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value) / "Charity_Contacts"
    fallback = Path.home() / "OneDrive"
    if fallback.is_dir():
        return fallback / "Charity_Contacts"
    raise ValueError(
        "OneDrive folder not found. Pass --onedrive-folder with the full path "
        "to your OneDrive destination, or --local-output to save locally."
    )


def remove_backup(folder: Path, names: list[str], marker_text: str) -> None:
    """Remove only recorded files, retrying Windows read-only/sync errors.

    Keep the marker after a partial failure so the next run can retry cleanup.
    Never change permissions on the parent OneDrive folder or unrelated files.
    """
    marker = folder / ".charity_backup.json"

    def remove_one(path: Path, directory: bool = False) -> None:
        operation = path.rmdir if directory else path.unlink
        try:
            operation()
        except FileNotFoundError:
            return
        except PermissionError:
            # Windows maps S_IWRITE to clearing the read-only attribute.
            # Preserve other mode bits (including directory traversal on POSIX).
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            operation()

    for attempt in range(4):
        try:
            if not folder.exists():
                return
            # Recheck the directory before every retry; never remove added files.
            if folder.is_symlink() or any(
                entry.name not in {*names, marker.name} or entry.is_symlink()
                for entry in folder.iterdir()
            ):
                raise OSError("Backup contents changed; leaving this folder for manual review.")
            for name in names:
                remove_one(folder / name)
            remove_one(marker)
            remove_one(folder, directory=True)
            return
        except OSError as exc:
            # If the directory itself was locked after its files were removed,
            # restore its ownership marker for cleanup on a later run.
            if folder.is_dir() and not marker.exists():
                try:
                    marker.write_text(marker_text, encoding="utf-8")
                except OSError:
                    pass
            temporary = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
            if not temporary or attempt == 3:
                raise
            time.sleep(0.5 * (2 ** attempt))


def retry_backup_io(operation):
    """Give OneDrive time to release a temporary access/sharing lock."""
    for attempt in range(4):
        try:
            return operation()
        except OSError as exc:
            temporary = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
            if not temporary or attempt == 3:
                raise
            time.sleep(0.5 * (2 ** attempt))


CHANGE_REPORT_NAME = "change_report.txt"
# last_checked is the scrape date, so it changes every run and is not reported.
REPORT_FIELDS = tuple(f for f in ORGANIZATION_OUTPUT_FIELDS if f not in ("org_id", "last_checked"))


def backup_time(name: str) -> str:
    try:
        return datetime.strptime(name, "backup_%Y%m%dT%H%M%S_%fZ").strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return name


def report_value(value: Any) -> str:
    value = clean_text(value)
    if not value:
        return "(blank)"
    return f'"{value[:150]}..."' if len(value) > 150 else f'"{value}"'


def build_change_report(organizations: list[dict[str, Any]], previous: tuple[Path, list[str]] | None,
                        run_name: str) -> str:
    """Plain-text report: each changed or new charity, then a count of the unchanged ones."""
    lines = ["Charity Contacts Change Report", "=" * 30,
             f"This run:      {run_name} ({backup_time(run_name)})"]
    if previous is None:
        lines += ["Compared with: nothing (no earlier backup was found)", "",
                  "This is the first saved run, so there is nothing to compare yet.",
                  f"{len(organizations)} charities were saved."]
        return "\n".join(lines) + "\n"
    folder, names = previous
    lines.append(f"Compared with: {folder.name} ({backup_time(folder.name)})")
    try:
        old_records = json.loads((folder / names[0]).read_text(encoding="utf-8"))
        if not isinstance(old_records, list):
            raise ValueError("not a list of charities")
    except (OSError, ValueError) as exc:
        lines += ["", f"The earlier backup could not be read ({exc}), so nothing was compared."]
        return "\n".join(lines) + "\n"

    key = lambda record: clean_text(record.get("org_id")) or clean_text(record.get("org_name"))
    old_by_key = {key(record): record for record in old_records if isinstance(record, dict)}
    body, changed, unchanged, added = [], 0, 0, 0
    for record in organizations:
        label = f"{record.get('org_name') or '(no name)'} ({record.get('org_id') or 'no ID'})"
        old = old_by_key.pop(key(record), None)
        if old is None:
            added += 1
            body.append([f"{label}: New charity (not in the previous run)"])
            continue
        # Fields an older backup did not have yet are skipped, not reported.
        differences = [f"    {field}: {report_value(old[field])} -> {report_value(record.get(field))}"
                       for field in REPORT_FIELDS
                       if field in old and clean_text(old[field]) != clean_text(record.get(field))]
        if differences:
            changed += 1
            body.append([label, *differences])
        else:
            unchanged += 1
    lines += [f"Fields compared: {', '.join(REPORT_FIELDS)}",
              "(last_checked is not compared because it is updated on every run.)", "",
              f"Summary: {len(organizations)} charities | {changed} changed | {unchanged} no change | "
              f"{added} new | {len(old_by_key)} removed", ""]
    # A blank line between charities keeps each one's changes easy to read.
    for block in body:
        lines += [*block, ""]
    if not body:
        lines += ["No charities changed.", ""]
    lines.append(f"({unchanged} {'charity' if unchanged == 1 else 'charities'} had no change)")
    if old_by_key:
        lines += ["", "Removed since the previous run (no longer in the target list):"]
        lines += [f"    {old.get('org_name') or '(no name)'} ({old.get('org_id') or 'no ID'})"
                  for old in old_by_key.values()]
    return "\n".join(lines) + "\n"


def save_backup_pair(folder: Path, records: list[dict[str, Any]],
                     organization_name: str, metadata_name: str) -> tuple[Path, Path]:
    """Save in the final folder and mark complete only after both files close.

    Never rename a folder inside OneDrive. First retain a local recovery copy
    so a sync/permission failure cannot discard the results of a long scrape.
    Only a fully saved backup triggers retention cleanup.
    """
    recovery_base = (Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA")
                     else Path.home()) / "Charity_Contacts_Recovery"
    recovery_base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="unsynced_", dir=recovery_base))
    organizations, metadata = split_records(records)
    write_json(staging / organization_name, organizations)
    write_json(staging / metadata_name, metadata)
    backup = folder / ("backup_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
    # Compare with the newest complete backup before this one is added.
    try:
        previous = (managed_backups(folder) or [None])[-1]
    except OSError:
        previous = None
    (staging / CHANGE_REPORT_NAME).write_text(
        build_change_report(organizations, previous, backup.name), encoding="utf-8")
    files = [organization_name, metadata_name, CHANGE_REPORT_NAME]
    marker = ".charity_backup.json"
    marker_text = json.dumps({
        "format": "charity_contacts_backup_v1",
        "files": files,
    })
    try:
        retry_backup_io(lambda: folder.mkdir(parents=True, exist_ok=True))
        retry_backup_io(lambda: backup.mkdir(exist_ok=True))
        for name in files:
            retry_backup_io(lambda name=name: shutil.copyfile(staging / name, backup / name))
        # The completion marker is written last. Unmarked partial folders are
        # ignored by cleanup and never displace an older complete backup.
        retry_backup_io(lambda: (backup / marker).write_text(marker_text, encoding="utf-8"))
    except OSError as exc:
        print(f"Could not finish saving to OneDrive: {exc}")
        print(f"Both JSON files and the change report are safe in this local recovery folder: {staging}")
        print("No old backups were deleted. Close open files or let OneDrive finish "
              "syncing, then copy the recovery files into OneDrive.")
        return staging / organization_name, staging / metadata_name
    shutil.rmtree(staging, ignore_errors=True)
    print(f"Wrote change report to {backup / CHANGE_REPORT_NAME}")
    try:
        cleanup_old_backups(folder)
    except OSError as exc:
        # Retention trouble must not turn a completed save into a failed scrape.
        print(f"Both JSON files were saved to {backup}, but backup cleanup failed: {exc}")
    return backup / organization_name, backup / metadata_name


def request_admin_cleanup(folder: Path) -> bool:
    """Request Windows UAC elevation for cleanup only, never for Chrome."""
    if sys.platform != "win32":
        return False

    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script_path = Path(__file__).resolve()
    arguments = subprocess.list2cmdline([
        str(script_path), "--cleanup-backups-only", str(folder.resolve()),
    ])
    # Encode a PowerShell program with literal paths; shell=False prevents
    # cmd.exe expansion. Start-Process receives a single quoted argument list.
    command = (
        "$ErrorActionPreference = 'Stop'; try { "
        "$cleanupProcess = Start-Process -FilePath " + ps_literal(sys.executable)
        + " -ArgumentList " + ps_literal(arguments)
        + " -WorkingDirectory " + ps_literal(str(script_path.parent))
        + " -Verb RunAs -Wait -PassThru; exit $cleanupProcess.ExitCode "
        "} catch { Write-Error $_ -ErrorAction Continue; exit 1 }"
    )
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    print("Windows blocked backup cleanup. Approve the UAC prompt to delete older "
          "backups as administrator; Chrome will keep running normally.", flush=True)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=False,
        )
    except OSError as exc:
        print(f"Could not start administrator cleanup: {exc}")
        return False
    if result.returncode == 0:
        print("Administrator backup cleanup finished.")
        return True
    print("Administrator cleanup was cancelled or failed. New JSON files remain saved; "
          "older backups may remain. Close open files and let OneDrive finish syncing.")
    return False


def managed_backups(folder: Path) -> list[tuple[Path, list[str]]]:
    """Complete backups this scraper created, oldest first, with their files.

    A backup holds the two JSON files and, from newer runs, the change report.
    Anything else inside (or a missing/odd marker) means it is not ours to use
    or delete.
    """
    marker = ".charity_backup.json"
    managed = []
    if not folder.is_dir():
        return managed
    for candidate in folder.iterdir():
        if (candidate.is_symlink() or not candidate.is_dir()
                or not re.fullmatch(r"backup_\d{8}T\d{6}_\d{6}Z", candidate.name)):
            continue
        try:
            info = json.loads((candidate / marker).read_text(encoding="utf-8"))
            names = info.get("files", [])
            if (info.get("format") != "charity_contacts_backup_v1"
                    or len(names) not in (2, 3) or len(set(names)) != len(names)
                    or any(not isinstance(name, str) or Path(name).name != name for name in names)
                    or not all(name.endswith(".json") for name in names[:2])
                    or names[2:] not in ([], [CHANGE_REPORT_NAME])
                    or not {entry.name for entry in candidate.iterdir()} <= {*names, marker}
                    or any(not entry.is_file() or entry.is_symlink()
                           for entry in candidate.iterdir())):
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        managed.append((candidate, names))
    return sorted(managed, key=lambda pair: pair[0].name)


def cleanup_old_backups(folder: Path, allow_elevation: bool = True) -> bool:
    """Recheck ownership and keep the newest two, optionally elevating on denial."""
    marker = ".charity_backup.json"
    managed = [path for path, _ in managed_backups(folder)]
    failed = False
    needs_admin = False
    for old in sorted(managed, key=lambda path: path.name)[:-2]:
        try:
            marker_text = (old / marker).read_text(encoding="utf-8")
            remove_backup(old, json.loads(marker_text)["files"], marker_text)
            print(f"Deleted oldest backup: {old}")
        except OSError as exc:
            failed = True
            needs_admin |= isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5
            print(f"Could not remove old backup {old}: {exc}")
    if needs_admin and allow_elevation and sys.platform == "win32":
        # One prompt for the whole run. The elevated child re-scans backups
        # and cannot start scraping or recursively request elevation.
        return request_admin_cleanup(folder)
    if failed:
        print("New JSON files remain saved. Close files open in the backup folder, "
              "let OneDrive finish syncing, then rerun. More than two backups may "
              "remain until Windows allows deletion.")
    return not failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract public charity profiles and contact details into reviewable JSON."
    )
    parser.add_argument(
        "--input",
        default="charity_scrape_targets.json",
        help="Markdown table or JSON target list (default: charity_scrape_targets.json).",
    )
    parser.add_argument(
        "--output",
        default="charity_contacts_live.json",
        help="Organization filename in each OneDrive backup; full path with --local-output or --export-targets.",
    )
    parser.add_argument(
        "--metadata-output",
        default=None,
        help="Metadata filename in each OneDrive backup; full path with --local-output. "
             "Default: Charity_Contacts_metadata.json. "
             "Not used with --export-targets.",
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--onedrive-folder", default=None,
        help="Full local OneDrive destination path. Default: Charity_Contacts in detected OneDrive.",
    )
    destination.add_argument(
        "--local-output", action="store_true",
        help="Save to --output/--metadata-output paths locally without backup rotation.",
    )
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Convert existing source-table values to JSON without visiting websites.",
    )
    parser.add_argument(
        "--export-targets",
        action="store_true",
        help="Write the cleaned scraper target list and do not visit websites.",
    )
    parser.add_argument(
        "--max-contact-pages",
        type=int,
        default=2,
        choices=range(0, 4),
        metavar="{0,1,2,3}",
        help="Maximum same-domain contact/location pages to check per organization (default: 2).",
    )
    parser.add_argument(
        "--max-profile-pages", type=int, default=2, choices=range(0, 6),
        help="Additional same-site About/Mission/Services pages (default: 2).",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.0,
        help="Pause between page visits in live mode (default: 1.0).",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run Chrome visibly instead of in headless mode.",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Chrome browsers to run at once; each uses roughly 0.5 GB of RAM (default: 4).",
    )
    parser.add_argument(
        "--fetch-mode", choices=("browser", "auto"), default="browser",
        help="browser: load every page in Chrome. auto: try plain HTTP first and use Chrome "
             "only for sites that need it (much faster; compare its output once before relying on it).",
    )
    parser.add_argument(
        "--http-workers", type=int, default=16,
        help="Sites fetched at once over plain HTTP in auto mode (default: 16).",
    )
    parser.add_argument(
        "--page-timeout", type=float, default=30.0,
        help="Seconds to wait for a page before recording an error (default: 30).",
    )
    parser.add_argument(
        "--chrome-path", default=None,
        help="Path to the Chrome/Chromium executable if Botasaurus cannot find it.",
    )
    parser.add_argument(
        "--max-per-contact-type", type=int, default=5,
        help="Keep the N best-graded addresses, phones, emails, and map links per organization; "
             "0 keeps every valid one with its score (default: 5).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N organizations (useful for a small test run).",
    )
    parser.add_argument("--cleanup-backups-only", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.cleanup_backups_only is not None:
        return args
    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be nonnegative.")
    if args.workers < 1 or args.http_workers < 1:
        parser.error("--workers and --http-workers must be at least 1.")
    if args.max_per_contact_type < 0:
        parser.error("--max-per-contact-type must be 0 or more.")
    if args.page_timeout <= 0:
        parser.error("--page-timeout must be positive.")
    if args.inventory_only and args.export_targets:
        parser.error("Choose either --inventory-only or --export-targets, not both.")
    if not args.export_targets:
        if args.local_output:
            metadata_path = Path(args.metadata_output) if args.metadata_output else Path(args.output).with_name("Charity_Contacts_metadata.json")
            protected_paths = {Path(args.input).resolve(), Path(args.output).resolve()}
            if metadata_path.resolve() in protected_paths:
                parser.error("The metadata output must differ from the input and organization output paths.")
            if Path(args.input).resolve() == Path(args.output).resolve():
                parser.error("The organization output must differ from the input path.")
        else:
            if Path(args.output).name.casefold() == Path(args.metadata_output or "Charity_Contacts_metadata.json").name.casefold():
                parser.error("Organization and metadata filenames must differ.")
            for name in (Path(args.output).name, Path(args.metadata_output or "Charity_Contacts_metadata.json").name):
                if not name.lower().endswith(".json") or name.startswith("."):
                    parser.error("Output filenames must be non-hidden .json files.")
            try:
                args.backup_folder = onedrive_destination(args.onedrive_folder)
            except ValueError as exc:
                parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    if args.cleanup_backups_only is not None:
        # Separate cleanup process: no target loading, scraping, or new backups.
        try:
            success = cleanup_old_backups(Path(args.cleanup_backups_only), allow_elevation=False)
        except OSError as exc:
            print(f"Backup cleanup failed: {exc}")
            success = False
        raise SystemExit(0 if success else 1)
    input_path = Path(args.input)
    output_path = Path(args.output)
    
    items = load_items(input_path)
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1.")
        items = items[: args.limit]

    started = time.monotonic()
    if args.export_targets:
        records = items
        mode = "target list"
    elif args.inventory_only:
        records = build_inventory(items)
        mode = "source inventory"
    else:
        records = run_live_scrape(
            items,
            args.max_contact_pages,
            args.delay_seconds,
            headless=not args.show_browser,
            max_profile_pages=args.max_profile_pages,
            workers=args.workers,
            fetch_mode=args.fetch_mode,
            http_workers=args.http_workers,
            page_timeout=args.page_timeout,
            chrome_path=args.chrome_path,
            max_per_type=args.max_per_contact_type,
        )
        mode = "live scrape"

    if args.export_targets:
        write_json(output_path, records)
    elif not args.local_output:
        output_path, metadata_path = save_backup_pair(
            args.backup_folder, records, output_path.name,
            Path(args.metadata_output or "Charity_Contacts_metadata.json").name,
        )
        print(f"Wrote {len(records)} contact/metadata records to {metadata_path}")
    else:
        metadata_path = (Path(args.metadata_output) if args.metadata_output
                         else output_path.with_name("Charity_Contacts_metadata.json"))
        organizations, metadata = split_records(records)
        write_json(output_path, organizations)
        write_json(metadata_path, metadata)
        print(f"Wrote {len(metadata)} contact/metadata records to {metadata_path}")
    print(f"Wrote {len(records)} {mode} records to {output_path} in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
