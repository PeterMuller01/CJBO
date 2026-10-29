# Charity contact scraper

This package turns the 58 organization websites in the target list into two
JSON files for the CJB Outreach Finding Charity database. For each
organization it collects public phone numbers, email addresses,
mailing/physical addresses, and a contact-form URL when one is found, grades
every value, and puts the best one of each into the main database fields.

The scraper does not try to defeat protections or infer missing data. It
checks the supplied page and at most two same-domain Contact/Location pages,
with a one-second pause between page visits to the same site. It also removes
addresses for Meg's House (CF-036) and My Sister's House (CF-037), because
their shelter locations are confidential.

## Files

- `charity_gui.py`: a desktop window for editing the charity list and running
  the scraper (see "Desktop app").
- `scrape_charity_contacts.py`: the Botasaurus scraper and JSON converter.
- `compare_outputs.py`: compares two output files record by record.
- `charity_scrape_targets.json`: the 58-site input list. Each entry has only
  `org_id`, `org_name`, `category`, `org_description`, `website_url`,
  `org_phone`, `org_email`, `street_address`, `city`, `state`, and `zip`
  (`is_active` and `last_checked` are set by each scrape). To add an
  organization, add an entry with these fields (at least `org_id`,
  `org_name`, and `website_url`).

## Desktop app

For a point-and-click way to manage the list and run the scraper:

```bash
python -m pip install -r requirements.txt
python charity_gui.py
```

Tkinter comes with the standard Windows and Mac Python installers; on Linux,
install your distribution's `python3-tk` package. The window has three tabs:

- **Charities** edits `charity_scrape_targets.json` (or any list opened with
  "Open list..."). Pick a charity to edit it, or press "New charity". Only the
  website URL is required (`https://` is added if left off); the name,
  category, and description are recommended, and every other field can be
  filled in when the client has it. A blank ID gets the next `CF-###` number.
  The form checks for a repeated website or ID and for badly formatted email,
  phone, state, and ZIP values. "Add to list"/"Update charity" changes
  the list in the window; "Save list" writes the file.
- **Database** shows the spreadsheet Power Automate builds from the live file.
  It opens the newest `.xlsx` in this folder that has `org_id` and
  `website_url` columns ("Choose spreadsheet..." picks another, "Reload" reads
  it again). Pick a charity to see all its fields; every field except the ID,
  active status, and last-checked date can be edited. "Save changes to
  database" writes only the edited cells, re-reading the file first so
  anything Power Automate wrote meanwhile is kept, and keeps the Excel table
  as it is. If the spreadsheet is open in Excel or still syncing, it says so
  and saves nothing. Edits to fields the charity list also has (name,
  category, description, website, phone, email, address) are saved to
  `charity_scrape_targets.json` too, so the next scrape keeps them: the
  name, website, description, and category always come from the list, while
  a phone, email, or address from the list is graded with everything found
  on the website, so a website value seen on more pages can still replace it.
  The app never copies scraped data into the spreadsheet; that only happens
  through Power Automate. Needs `openpyxl` (in `requirements.txt`).
- **Run** chooses the options and runs the scraper. It covers the charity list
  file, a test run of the first few charities (which switches to local files
  so it does not use up a OneDrive backup), OneDrive backups or local files
  and how many backups to keep,
  the number of browsers (with a memory estimate for this computer), the
  faster fetch mode, and the advanced options under "Show advanced options".
  The command it will run is shown, the log and progress update live, Stop
  ends the run and its browsers, and when it finishes the change report and
  output folder open with one click. If the list has unsaved changes, it
  offers to save them first.

The app runs the same `scrape_charity_contacts.py` command described below,
so the two always behave the same.

## Run a fresh scrape

Open a terminal in this folder, then run:

```bash
python -m pip install -r requirements.txt
python scrape_charity_contacts.py --input charity_scrape_targets.json --output charity_contacts_live.json
```

This writes two files, each a JSON array with one record per organization in
the same order (see "Where the files are saved" for the location):

- `charity_contacts_live.json` (`--output`): only the database fields, for
  the Power Automate import: `org_id`, `org_name`, `category`,
  `org_description`, `website_url`, `org_phone`, `org_email`,
  `street_address`, `city`, `state`, `zip`, `is_active`, and `last_checked`.
- `Charity_Contacts_metadata.json` (`--metadata-output`): everything else, matched by `org_id` and `org_name`.
  It has every graded value (`contacts.phones`, `contacts.addresses`,
  `contacts.emails`), the scrape status and review notes (`extraction`), the
  description/category values found on the website (`organization_details`),
  where each main field came from (`field_sources`), and the target file's
  values (`original_fields`).

### Where the main fields come from

- `org_id`, `org_name`, and `website_url` always come from the target file;
  they are never taken from the website.
- `org_description` and `category` use the target file's value when it has
  one. If the target leaves them blank, the best value found on the website is
  used (a category guessed from the site's wording is flagged with
  `extraction.profile_review_required`). Either way, the values found on the
  website are kept in the metadata file under `organization_details`.
- Phone, email, and address fields use the top-graded website value (see
  "How contacts are graded"); if nothing valid is found, the target file's
  value is kept.
- `is_active` and `last_checked` come from the scrape itself (see below);
  `last_checked` is the date of the run, even when the site could not be read.

For a small first test, add `--limit 3 --local-output` (see below for why
`--local-output`). If a site behaves differently in headless Chrome, retry
with `--show-browser`; do not try to bypass a site's access protections.

### Where the files are saved

By default each run is saved as a backup in OneDrive:

- The folder is `Charity_Contacts` inside the OneDrive folder Windows reports
  (`OneDriveCommercial`, `OneDrive`, or `OneDriveConsumer`), or
  `~/OneDrive/Charity_Contacts`. Use `--onedrive-folder PATH` to choose
  another folder. If no OneDrive folder is found, the run stops before
  scraping and asks for `--onedrive-folder` or `--local-output`.
- Each run creates `backup_<UTC date and time>` with both JSON files (named by
  `--output` and `--metadata-output`), a `change_report.txt` (see "Change
  report" below), and a `.charity_backup.json` marker.
- After a **full** run (no `--limit`, not `--inventory-only`) saves
  successfully, its `charity_contacts_live.json` is also copied to the
  `Charity_Contacts` folder itself, replacing the previous copy; that is the
  file the Power Automate flow imports. Test and inventory runs never replace
  it, so Power Automate never imports a partial list.
- Only the newest backups are kept, 2 by default (`--keep-backups N` to keep
  more): after a run saves successfully, the oldest backup folders beyond
  that number are deleted. Only folders the scraper created (with the marker
  and nothing else inside) are ever deleted; other files in the folder are
  left alone.
- The files are first written to a local recovery folder
  (`%LOCALAPPDATA%\Charity_Contacts_Recovery`). If OneDrive cannot be written,
  they stay there, nothing old is deleted, and the run says where they are.
- If Windows blocks deleting an old backup, the scraper asks for administrator
  approval (a UAC prompt) for the cleanup only.

### Change report

Each backup has a `change_report.txt` comparing this run's main file with the
most recent earlier backup's (however many backups are kept), matching
charities by `org_id`. It lists only the charities
that changed (in run order, with a blank line between them), then how many had
no change:

```text
Summary: 58 charities | 2 changed | 56 no change | 0 new | 0 removed

Carolina Family Services (CF-003)
    org_phone: "Not found" -> "(864) 283-0637"
    street_address: "301 Anderson St." -> "17 Memorial Medical Drive"

Children's Place (CF-007)
    org_phone: "Not listed" -> "(803) 641-4144"
    street_address: "137 Prosperity Ln." -> "155 Prosperity Lane NE"

(56 charities had no change)
```

- A changed charity lists each changed field with its old and new value (long
  values are shortened to 150 characters).
- Charities added to the target list since the previous run are marked "New
  charity"; ones no longer in the list are listed under "Removed" at the end.
- `last_checked` is not compared, because it changes on every run.
- The first run has nothing to compare with, and its report says so.
- `--local-output` runs do not make backups, so they do not make a report.

Every run counts toward the kept backups, including test runs with `--limit` and
`--inventory-only` runs, so a small test run can push out a full backup. Use
`--local-output` for those: it saves to the `--output` and
`--metadata-output` paths you give (metadata defaults to
`Charity_Contacts_metadata.json` next to `--output`) and does not touch the
backups.

## Scrape status and is_active

`extraction.status` means:

- `success`: an address, a phone number, and an email were all found.
- `partial`: some were found, or a page had an error.
- `not_found` / `manual_review`: the site loaded but no contacts were found.
- `failed`: the site could not be used: it did not load, redirected to
  another domain, showed an error page (for example "WordPress › Error" or
  "404"), or showed a bot check such as "Just a moment...". The reason is in
  `extraction.errors`.

`is_active` is set from that status:

- `success` or `partial`: `Yes`.
- anything else: `Unsuccessful check, manual review`. The site may only have
  been down during the check, so a person should look at it later.

## How contacts are graded

Every valid phone number, address, and email is given points, a confidence
label, and the reasons for its score, and the 5 best of each are kept. The main idea: an
organization's own number or address is usually repeated on most of its pages
(header, footer, contact page), while a partner's or program's appears once.

| Signal | Points |
| --- | --- |
| Shown on a checked page (per page, up to 5 pages) | +2 each |
| Mentioned again on those pages (up to 3) | +1 each |
| Matches the client's existing list | +3 |
| Clickable phone/email link, or the site's structured data | +2 |
| In an address/contact section, or a map link | +1 |
| On the home page or a contact page | +1 |
| Address in the client's state / in a different state | +1 / -3 |
| Email on the organization's own website domain | +2 |
| General inbox (`info@`, `contact@`, ...) / special-purpose inbox (web admin, volunteer, donations, ...) | +1 / -2 |
| Phone link that dials a different number than it shows | -3 |
| Well-known national hotline (RAINN, trafficking, Childhelp, ...) | -6 |

Confidence: `high` is 9 or more points, `medium` 5 to 8, `low` 4 or less.

The top-scoring phone and email are marked `"primary": true` and fill
`org_phone` and `org_email`. For the address, the top-scoring street address
with medium or high confidence is primary (a place people can visit), and a
PO Box is used only when there is none. `field_sources` shows the score and
confidence of each primary value.

Each entry looks like this:

```json
{
  "raw": "(864) 227-1623",
  "normalized": "+18642271623",
  "labeled_hotline": true,
  "score": 17,
  "confidence": "high",
  "primary": true,
  "reasons": ["+8 shown on 4 of 4 pages checked", "+3 mentioned 13 times in all",
              "+1 on the home page or a contact page", "+2 clickable phone/email link",
              "+3 matches the client's list", "labeled on the site as a hotline/24-7/crisis line"]
}
```

Before grading, values are checked and merged:

- **Addresses** must be complete (street or PO Box, city, state, ZIP). Street
  fragments and page text that only looks like an address ("1715 Clerk of
  Court") are dropped, and versions of the same place ("Avenue" vs "Ave.")
  are merged.
- **Phone numbers** must be valid US numbers. Formats are merged and every
  number is written as `(803) 555-0100`. Hotlines are graded like any other
  number; `labeled_hotline` says the site labels it as a 24/7 or crisis line.
- **Emails** that are image names, placeholders, cut off, or an individual's
  personal Gmail/Yahoo/etc. address are dropped. Free-mail addresses that are
  clearly the organization's (like `defendersforchildren@gmail.com`) are kept.
- **The client's existing values are confirmed anywhere on a page.** The
  scraper normally reads plain text only in footers and contact sections, so
  it does not pick up partners' details. A phone, email, or street that is
  already in the client's list is also looked for in the full page text;
  those entries show the method `confirmed_on_page` (or end in
  `_confirms_provided` for addresses).
- **Phone links that dial a different number than they show** are noted in
  `extraction.notes`; the shown number is graded normally.
- Staff, board, directory, news, and event pages are not crawled.

`extraction.contact_review` lists anything a person should check, for
example "Primary phone (877) 969-0010 differs from the client's list". National
organizations, chapters, and government offices often repeat a head-office
number on every page, so their primary phone can outscore the local one; this
note catches those cases.

Use `--max-per-contact-type N` to keep a different number of entries of each
type (`0` keeps every valid one).

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
| `--chrome-path PATH` | none | Chrome/Chromium executable, if Botasaurus cannot find it. |
| `--max-per-contact-type N` | 5 | Best-graded addresses, phones, emails, and map links kept per organization; `0` keeps all. |

If Chrome crashes during a run, that site is retried in a fresh browser
instead of every remaining site failing.

`--fetch-mode auto` reads the HTML the server sends instead of the page after
Chrome has run its scripts. For most sites that is the same content, but some
sites only reveal email addresses with JavaScript, so compare once on the real
sites before relying on it:

```bash
python scrape_charity_contacts.py --local-output --output run_browser.json --metadata-output run_browser_meta.json
python scrape_charity_contacts.py --local-output --fetch-mode auto --output run_auto.json --metadata-output run_auto_meta.json
python compare_outputs.py run_browser.json run_auto.json
python compare_outputs.py run_browser_meta.json run_auto_meta.json
```

`extraction.fetch_method` in each metadata record says whether it came from
`http` or `browser`.

## Rebuild the included JSON files

If you have the original Markdown table in the same folder, run the commands
below. `--inventory-only` uses `--local-output` and its own
`--metadata-output`, so it does not create a backup or replace a live scrape's
metadata file.

```bash
python scrape_charity_contacts.py --input "Pasted markdown(2).md" --export-targets --output charity_scrape_targets.json
python scrape_charity_contacts.py --input "Pasted markdown(2).md" --inventory-only --local-output --output charity_contact_inventory.json --metadata-output charity_contact_inventory_metadata.json
```

Use `--max-contact-pages 1` to reduce the crawl to the supplied page plus one
same-site contact/location page.
