#!/usr/bin/env python3
"""Collect public charity organization profiles and contacts with Botasaurus.

The script accepts either the supplied Markdown table or a cleaned JSON target
list.  It preserves every public address, phone number, and email it finds
instead of silently selecting only one value.

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
import json
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


FIELDS = ('org_name', 'org_type', 'category', 'org_description', 'area_served',
          'website_url', 'ein', 'is_active')
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
        *FIELDS, 'org_phone', 'hotline_24_7', 'org_email', 'street_address',
        'city', 'state', 'zip', 'last_checked', 'org_notes')}
    result['field_sources'] = {
        field: dict(value=value, source_url=None,
                    method='provided_inventory' if value else 'not_found')
        for field, value in result['original_fields'].items()
    }


def ein(value):
    value = text(value)
    match = re.fullmatch(r'(\d{2})-?(\d{7})', value)
    return f'{match[1]}-{match[2]}' if match else None


def place_names(value):
    if isinstance(value, list):
        return '; '.join(filter(None, (place_names(v) for v in value)))
    if isinstance(value, dict):
        return text(value.get('name') or value.get('addressLocality') or value.get('addressRegion'))
    return text(value)


def structured(result, obj, url):
    """Caller has already matched this entity to the target organization."""
    for field, key in [('org_name', 'name'), ('org_description', 'description'),
                       ('area_served', 'areaServed'), ('category', 'category')]:
        value = place_names(obj.get(key))
        add(result, field, value[:600] if field == 'org_description' else value,
            url, 'matched_json_ld', 100)
    if not obj.get('areaServed'):
        add(result, 'area_served', place_names(obj.get('serviceArea')), url, 'matched_json_ld', 100)
    kind = obj.get('@type', [])
    kinds = {str(v).lower() for v in (kind if isinstance(kind, list) else [kind])}
    if kinds & {'nonprofitorganization', 'ngo', 'charity'} or obj.get('nonprofitStatus'):
        add(result, 'org_type', 'Nonprofit', url, 'matched_json_ld', 100)
    elif 'governmentorganization' in kinds:
        add(result, 'org_type', 'Government agency', url, 'matched_json_ld', 100)
    # Generic Organization/LocalBusiness does not establish legal type.
    for key in ('taxID', 'taxId'):
        value = ein(obj.get(key))
        if value:
            add(result, 'ein', value, url, 'matched_json_ld_tax_id', 100)
    identifiers = obj.get('identifier', [])
    for value in identifiers if isinstance(identifiers, list) else [identifiers]:
        if isinstance(value, dict) and re.search(r'\bEIN\b|employer identification', text(value.get('propertyID') or value.get('name')), re.I):
            add(result, 'ein', ein(value.get('value')), url, 'matched_json_ld_ein', 100)
    website = obj.get('url')
    if isinstance(website, str) and host(website) == host(result['original_fields']['website_url']):
        add(result, 'website_url', website, url, 'matched_json_ld', 100)


def extract(result, soup, url):
    root = result['original_fields']['website_url']
    is_home = urlparse(url).path.rstrip('/') == ''
    # Organization branding is safer than arbitrary page headings/titles.
    branding = soup.select_one('meta[property="og:site_name"]')
    if branding:
        value = text(branding.get('content'))
        seed = text(result['original_fields']['org_name']).casefold()
        words = set(re.findall(r'[a-z0-9]{4,}', seed))
        if not seed or value.casefold() in seed or seed in value.casefold() or len(words & set(re.findall(r'[a-z0-9]{4,}', value.casefold()))) >= 2:
            add(result, 'org_name', value, url, 'site_name_meta', 70)
    if is_home:
        description = soup.select_one('meta[name="description"], meta[property="og:description"]')
        if description:
            add(result, 'org_description', text(description.get('content'))[:600], url, 'homepage_description', 70)
    canonical = soup.select_one('link[rel="canonical"]')
    if is_home and canonical:
        value = urljoin(url, text(canonical.get('href')))
        if host(value) == host(root):
            add(result, 'website_url', value, url, 'homepage_canonical', 80)
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
            if re.search(rf'\b{subject}\s+(?:is|are)\s+(?:an?\s+)?(?:(?:registered|private|independent|community|charitable|501\(c\)\(3\))\s+)*(?:non[ -]?profit|not.for.profit)\b', block, re.I):
                add(result, 'org_type', 'Nonprofit', url, 'self_description_text', 80)
            if re.search(rf'\b{subject}\s+(?:is|are)\s+(?:an?\s+)?501\s*\(c\)\s*\(3\)', block, re.I):
                add(result, 'org_type', 'Nonprofit', url, 'self_description_text', 80)
            if re.search(rf'\b{subject}\s+(?:is|are)\s+(?:an?\s+)?(?:local |county |state |federal )?government agency\b', block, re.I):
                add(result, 'org_type', 'Government agency', url, 'self_description_text', 80)
            # Keep the service-area statement intact rather than guessing borders.
            area = re.search(r'[^.!?]*(?:\bwe serve\b|\bour service area\b|\bserving (?:the )?(?:residents|people|families|communities|counties)\b)[^.!?]*[.!?]?', block, re.I)
            if area:
                add(result, 'area_served', area[0].strip(), url, 'service_area_statement', 80)
            if re.search(rf'\b{subject}\s+(?:is|are|has|have)\s+(?:now |permanently )?(?:closed|ceased operations|no longer operating)\b', block, re.I):
                add(result, 'is_active', 'No', url, 'explicit_operating_statement', 100)
            elif re.search(rf'\b{subject}\s+(?:(?:is|are)\s+(?:currently )?(?:open|operating)|(?:continues?|continue) to (?:operate|provide services))\b', block, re.I):
                add(result, 'is_active', 'Yes', url, 'explicit_operating_statement', 80)
        # EIN needs a tax label; never take an arbitrary nine-digit number.
        # A block naming a different entity is still a reviewable candidate.
        match = re.search(r'\b(?:EIN|employer identification number|federal tax (?:ID|identification)(?: number)?)\s*[:#-]?\s*(\d{2}-?\d{7})\b', block, re.I)
        if match and (self_statement or mission or re.match(r'^(?:our\s+)?(?:EIN|employer identification|federal tax)', block, re.I)):
            add(result, 'ein', ein(match[1]), url, 'labeled_tax_id_text', 70)
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
        if any(word in f'{urlparse(value).path.lower()} {label}' for word in PROFILE_WORDS):
            if value not in pages and not re.search(r'\.(pdf|jpg|png|zip)$', urlparse(value).path, re.I):
                pages.append(value)
    return pages


def finalize(result):
    """Promote best profile evidence; keep source values as documented fallbacks."""
    for field in FIELDS:
        candidates = result['organization_details'][field]
        if candidates:
            chosen = max(candidates, key=lambda row: row['priority'])
            result[field] = chosen['value']
            result['field_sources'][field] = {k: v for k, v in chosen.items() if k != 'priority'}
        else:
            result['field_sources'][field] = dict(
                value=result[field], source_url=None,
                method='provided_inventory' if result[field] else 'not_found')
    # Scraped primary values use exactly the requested flat schema. All others
    # remain available in contacts and original_fields.
    for field, bucket, key in [('org_phone', 'phones', 'raw'),
                               ('hotline_24_7', 'hotlines_24_7', 'raw'),
                               ('org_email', 'emails', 'value')]:
        values = result['contacts'][bucket]
        if values:
            result[field] = values[0][key]
            result['field_sources'][field] = dict(values[0])
        else:
            result['field_sources'][field] = dict(value=result[field], source_url=None,
                method='provided_inventory' if result[field] else 'not_found')
    addresses = result['contacts']['addresses']
    if addresses:
        best = next((row for row in addresses if row['kind'] == 'physical'), addresses[0])
        raw = best['raw']
        locality = re.match(r'^(.*?),\s*([A-Za-z .\'-]+),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', raw)
        if locality:
            for field, value in zip(('street_address', 'city', 'state', 'zip'), locality.groups()):
                result[field] = value.strip()
                result['field_sources'][field] = dict(value=value.strip(), source_url=best['source_url'], method=best['method'])
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
        field for field in FIELDS if len({r['value'].casefold()
        for r in result['organization_details'][field]}) > 1]
    if result['extraction']['profile_conflicts']:
        result['extraction']['profile_review_required'] = True
    if result['data_origin'] == 'live_scrape' and result['extraction']['pages_checked']:
        result['last_checked'] = result['scraped_at'][:10]
        result['field_sources']['last_checked'] = dict(
            value=result['last_checked'], source_url=None, method='website_check_date')
    if result['org_id'] in {'CF-036', 'CF-037'}:
        for fields in (result, result['original_fields']):
            for field in ('street_address', 'city', 'state', 'zip', 'org_notes'):
                fields[field] = ''
        for field in ('street_address', 'city', 'state', 'zip', 'org_notes'):
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


def source_item(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a source row into the small, portable scraper input format."""
    existing = record.get("source_fields") or {}
    return {
        "org_id": clean_text(record.get("org_id")),
        "org_name": clean_text(
            record.get("organization_name") or record.get("org_name")
        ),
        "org_type": clean_text(record.get("org_type")),
        "category": clean_text(record.get("category")),
        "org_description": clean_text(record.get("org_description")),
        "area_served": clean_text(record.get("area_served")),
        "website_url": markdown_url(
            record.get("source_url") or record.get("website_url")
        ),
        "ein": clean_text(record.get("ein")),
        "is_active": clean_text(record.get("is_active")),
        "source_fields": {
            "org_phone": clean_text(existing.get("org_phone", record.get("org_phone"))),
            "hotline_24_7": clean_text(
                existing.get("hotline_24_7", record.get("hotline_24_7"))
            ),
            "org_email": clean_text(existing.get("org_email", record.get("org_email"))),
            "street_address": clean_text(
                existing.get("street_address", record.get("street_address"))
            ),
            "city": clean_text(existing.get("city", record.get("city"))),
            "state": clean_text(existing.get("state", record.get("state"))),
            "zip": clean_text(existing.get("zip", record.get("zip"))),
            "org_notes": clean_text(existing.get("org_notes", record.get("org_notes"))),
            "last_checked": clean_text(
                existing.get("last_checked", record.get("last_checked"))
            ),
        },
    }


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
    fields = item["source_fields"]
    result = {
        "org_id": item["org_id"],
        "org_name": item["org_name"],
        "org_type": item["org_type"],
        "category": item["category"],
        "org_description": item["org_description"],
        "area_served": item["area_served"],
        "website_url": item["website_url"],
        "org_phone": fields["org_phone"],
        "hotline_24_7": fields["hotline_24_7"],
        "org_email": fields["org_email"],
        "street_address": fields["street_address"],
        "city": fields["city"],
        "state": fields["state"],
        "zip": fields["zip"],
        "ein": item["ein"],
        "is_active": item["is_active"],
        "last_checked": fields["last_checked"],
        "org_notes": fields["org_notes"],
        "scraped_at": utc_now() if data_origin == "live_scrape" else None,
        "data_origin": data_origin,
        "contacts": {
            "addresses": [],
            "phones": [],
            "hotlines_24_7": [],
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


def unique_add(records: list[dict[str, Any]], candidate: dict[str, Any], key: str) -> None:
    """Append a contact only once, while retaining the first source location."""
    comparison = clean_text(candidate[key]).lower()
    if comparison and not any(clean_text(row[key]).lower() == comparison for row in records):
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
    fields = item["source_fields"]
    street = usable_value(fields.get("street_address"))
    city = usable_value(fields.get("city"))
    state = usable_value(fields.get("state"))
    zip_code = usable_value(fields.get("zip"))
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
                        "published": None,
                        "source_url": None,
                        "method": "provided_inventory",
                    },
                    "raw",
                )

        primary = usable_value(item["source_fields"].get("org_phone"))
        if primary:
            for phone in split_phone_values(primary):
                unique_add(
                    result["contacts"]["phones"],
                    {
                        "raw": phone,
                        "normalized": phone_normalized(phone),
                        "kind": "main",
                        "source_url": None,
                        "method": "provided_inventory",
                    },
                    "raw",
                )

        hotline = usable_value(item["source_fields"].get("hotline_24_7"))
        if hotline:
            for phone in split_phone_values(hotline):
                unique_add(
                    result["contacts"]["hotlines_24_7"],
                    {
                        "raw": phone,
                        "normalized": phone_normalized(phone),
                        "kind": "hotline",
                        "source_url": None,
                        "method": "provided_inventory",
                    },
                    "raw",
                )

        email = usable_value(item["source_fields"].get("org_email"))
        if email and EMAIL_RE.fullmatch(email):
            unique_add(
                result["contacts"]["emails"],
                {
                    "value": email.lower(),
                    "kind": "general",
                    "source_url": None,
                    "method": "provided_inventory",
                },
                "value",
            )
        elif clean_text(item["source_fields"].get("org_email")).lower() == "contact form only":
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
        r"^(?:address|location|mailing address|visit us|our office)\s*:?\s*",
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
                "published": True,
                "source_url": page_url,
                "method": method,
            },
            "raw",
        )


def add_phone(
    result: dict[str, Any], value: Any, page_url: str, method: str, context: str = ""
) -> None:
    for phone in split_phone_values(clean_text(value)):
        destination = (
            result["contacts"]["hotlines_24_7"] if phone_kind(context) == "hotline" else result["contacts"]["phones"]
        )
        unique_add(
            destination,
            {
                "raw": phone,
                "normalized": phone_normalized(phone),
                "kind": "hotline" if destination is result["contacts"]["hotlines_24_7"] else "main",
                "source_url": page_url,
                "method": method,
            },
            "raw",
        )


def add_email(result: dict[str, Any], value: Any, page_url: str, method: str) -> None:
    for email in EMAIL_RE.findall(clean_text(value)):
        unique_add(
            result["contacts"]["emails"],
            {
                "value": email.lower(),
                "kind": "general",
                "source_url": page_url,
                "method": method,
            },
            "value",
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
        context = f"{link_text(anchor.parent)} {link_text(anchor)}"
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
            nearby = container_text[max(0, match.start() - 80) : match.end() + 80]
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
            bool(contacts["phones"] or contacts["hotlines_24_7"]),
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
            result["extraction"]["pages_checked"].append(page_url)
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

    set_result_status(result)
    finalize(result)
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
) -> list[dict[str, Any]]:
    """Run the live scrape.

    ``browser`` mode loads every page in Chrome.  ``auto`` mode tries plain
    HTTP first and sends only the sites that need JavaScript, block plain
    requests, or yield no contacts to Chrome.
    """
    batch = [
        {**item, "max_contact_pages": max_contact_pages, "max_profile_pages": max_profile_pages}
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
        help="JSON output path.",
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
        "--skip-irs", action="store_true",
        help="Do not add IRS status fields (irs_status, irs_last_checked, irs_details).",
    )
    parser.add_argument(
        "--irs-cache-dir", default="irs_data",
        help="Where IRS bulk files are cached between runs (default: irs_data).",
    )
    parser.add_argument(
        "--irs-max-age-hours", type=float, default=24.0,
        help="Reuse cached IRS files younger than this without contacting the IRS (default: 24).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N organizations (useful for a small test run).",
    )
    args = parser.parse_args()
    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be nonnegative.")
    if args.workers < 1 or args.http_workers < 1:
        parser.error("--workers and --http-workers must be at least 1.")
    if args.page_timeout <= 0:
        parser.error("--page-timeout must be positive.")
    if args.inventory_only and args.export_targets:
        parser.error("Choose either --inventory-only or --export-targets, not both.")
    return args


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    
    items = load_items(input_path)
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1.")
        items = items[: args.limit]

    started = time.monotonic()
    irs_loader = None
    if not args.export_targets and not args.skip_irs:
        # Download/refresh the IRS files while the websites are being scraped.
        from irs_status import load_irs_data

        irs_loader = ThreadPoolExecutor(max_workers=1)
        irs_future = irs_loader.submit(load_irs_data, Path(args.irs_cache_dir), args.irs_max_age_hours)

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
        )
        mode = "live scrape"

    if irs_loader:
        from irs_status import apply_irs_status, summarize

        irs_data, irs_error = irs_future.result()
        irs_loader.shutdown()
        if irs_error:
            print(f"WARNING: IRS check skipped ({irs_error}); irs_status is 'not_checked'.")
        apply_irs_status(records, irs_data, irs_error)
        print(f"IRS status: {summarize(records)}")

    write_json(output_path, records)
    print(f"Wrote {len(records)} {mode} records to {output_path} in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
