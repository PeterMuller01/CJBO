# Charity contact scraper

This package turns the 58 organization websites in the supplied table into a
reviewable JSON file. It collects public phone numbers, 24/7 hotlines, email
addresses, mailing/physical addresses, and a contact-form URL when one is
found. It also checks each EIN against IRS bulk data (see "IRS status" below).

Every output record also keeps the original organization fields: ID, name,
organization type, category, description, area served, website URL, original
contact fields, address fields, EIN, active status, last-checked date, and
notes. Freshly scraped values are placed separately under `contacts`.

The scraper does not try to defeat protections or infer missing data. It
checks the supplied page and at most two same-domain Contact/Location pages,
with a one-second pause between page visits to the same site. It also removes
addresses for Meg's House (CF-036) and My Sister's House (CF-037), because
their shelter locations are confidential.

## Files

- `scrape_charity_contacts.py` — the Botasaurus scraper and JSON converter.
- `irs_status.py` — the IRS exempt-status check (also runs on its own).
- `compare_outputs.py` — compares two output files record by record.
- `charity_scrape_targets.json` — the cleaned 58-site input list.
- `charity_contact_inventory.json` — the existing contact values from the
  supplied table, converted to JSON. This is not a fresh web scrape.

## Run a fresh scrape

Open a terminal in this folder, then run:

```bash
python -m pip install -r requirements.txt
python scrape_charity_contacts.py --input charity_scrape_targets.json --output charity_contacts_live.json
```

The result is a JSON array. Each object includes `contacts.addresses`,
`contacts.phones`, `contacts.hotlines_24_7`, `contacts.emails`, and
`contacts.contact_form_url`. Contact values are arrays so the scraper does not
throw away extra offices or hotlines.

Each value records its `source_url` and `method` (for example, `tel_link`,
`mailto_link`, `json_ld`, or `semantic_address`). Review records marked
`partial`, `not_found`, `manual_review`, or `failed` before importing them.

For a small first test, add `--limit 3`. If a site behaves differently in
headless Chrome, retry with `--show-browser`; do not try to bypass a site's
access protections.

## Speed options

The scraper runs several sites at the same time. Each site is still crawled
one page at a time with the same pause, so no single website gets more
traffic than before.

| Option | Default | What it does |
| --- | --- | --- |
| `--workers N` | 4 | Chrome browsers running at once. Each uses about 0.5 GB of RAM. |
| `--fetch-mode auto` | `browser` | Fetch pages with plain HTTP first and open Chrome only for sites that need JavaScript, block plain requests, or return no contacts. Much faster. |
| `--http-workers N` | 16 | Sites fetched at once over plain HTTP in `auto` mode. |
| `--page-timeout S` | 30 | Seconds to wait for one page before recording an error. |
| `--chrome-path PATH` | — | Chrome/Chromium executable, if Botasaurus cannot find it. |

If Chrome crashes during a run, that site is retried in a fresh browser
instead of every remaining site failing.

`--fetch-mode auto` reads the HTML the server sends instead of the page after
Chrome has run its scripts. For most sites that is the same content, but
compare once on the real sites before relying on it:

```bash
python scrape_charity_contacts.py --output run_browser.json
python scrape_charity_contacts.py --fetch-mode auto --output run_auto.json
python compare_outputs.py run_browser.json run_auto.json
```

`extraction.fetch_method` in each record says whether it came from `http` or
`browser`.

## IRS status

Each record gets three new fields. `is_active` is **not** changed; the IRS
result is kept separate so it can be reviewed first.

- `irs_status`
  - `active` — EIN is in the IRS Exempt Organizations Business Master File
    and not currently revoked.
  - `revoked` — EIN is on the IRS automatic revocation list (no return filed
    for three years) with no later reinstatement.
  - `not_found` — EIN is in neither file. Check for a wrong EIN, or a chapter
    or church covered by a parent organization's group exemption.
  - `no_ein` — no EIN on file (government agencies).
  - `invalid_ein` — the EIN is not nine digits.
  - `not_checked` — the IRS files could not be downloaded.
- `irs_last_checked` — date the check ran (YYYY-MM-DD).
- `irs_details` — what the IRS files say: legal name, city/state, latest tax
  period filed, ruling date, revocation/reinstatement dates, the IRS data
  date, and `review_reasons` explaining anything that needs a human look
  (for example, an IRS legal name that does not resemble `org_name`).

The four nationwide BMF region files and the revocation list are downloaded
into `irs_data/` (about 300 MB) and reused for 24 hours
(`--irs-max-age-hours`). After that, the scraper asks the IRS whether the
files changed and downloads them again only if they did. The download
runs while the websites are being scraped. Use `--skip-irs` to turn it off.

To add or refresh IRS fields on an existing file without scraping:

```bash
python irs_status.py --input charity_contacts_live.json
```

## Rebuild the included JSON files

If you have the original Markdown table in the same folder, run:

```bash
python scrape_charity_contacts.py --input "Pasted markdown(2).md" --export-targets --output charity_scrape_targets.json
python scrape_charity_contacts.py --input "Pasted markdown(2).md" --inventory-only --output charity_contact_inventory.json
```

Use `--max-contact-pages 1` to reduce the crawl to the supplied page plus one
same-site contact/location page.
