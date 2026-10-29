#!/usr/bin/env python3
"""Desktop window for editing the charity list and running the scraper.

Two tabs:
    Charities  add, edit, and remove organizations in the target file.
    Run        choose options and run scrape_charity_contacts.py with a live log.

The scraper runs as a separate process, so the window stays responsive during
a long scrape and Stop can end it (and its Chrome browsers) at any time.

    python charity_gui.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from urllib.parse import urlparse

from scrape_charity_contacts import (
    EMAIL_RE, TARGET_FIELDS, US_STATES, onedrive_destination,
    phone_key, source_item, split_phone_values, usable_value, write_json,
)

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "scrape_charity_contacts.py"
DEFAULT_TARGETS = HERE / "charity_scrape_targets.json"
GB_PER_BROWSER = 0.5  # rough Chrome memory use per browser (same note as --workers)

# Form fields: (key, label, group).  Only the website is required.
FORM_FIELDS = (
    ("website_url", "Website URL", "Required"),
    ("org_name", "Organization name", "Recommended"),
    ("category", "Category", "Recommended"),
    ("org_description", "Description", "Recommended"),
    ("org_id", "Organization ID", "Optional"),
    ("org_phone", "Phone", "Optional"),
    ("org_email", "Email", "Optional"),
    ("street_address", "Street address", "Optional"),
    ("city", "City", "Optional"),
    ("state", "State", "Optional"),
    ("zip", "ZIP code", "Optional"),
)
FIELD_HINTS = {
    "website_url": "e.g. https://beyondabuse.info/",
    "org_id": "Leave blank to assign the next CF-### number",
    "org_phone": "e.g. (864) 227-1623",
    "state": "Two letters, e.g. SC",
}


# --- Charity list helpers (no window needed, so they are easy to test) ------

def normalize_url(url: str) -> str:
    url = url.strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def url_key(url: str) -> str:
    parsed = urlparse(url.lower())
    return parsed.netloc.removeprefix("www.") + parsed.path.rstrip("/")


def next_org_id(records: list[dict[str, str]]) -> str:
    numbers = [int(match[1]) for record in records
               if (match := re.fullmatch(r"CF-(\d+)", record.get("org_id", "")))]
    return f"CF-{max(numbers, default=0) + 1:03d}"


def validate_charity(values: dict[str, str], others: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one charity; ``others`` excludes itself."""
    errors, warnings = [], []
    url = values["website_url"]
    parsed = urlparse(url)
    if not url:
        errors.append("Website URL is required.")
    elif parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        errors.append("Website URL does not look like a web address.")
    elif any(url_key(other["website_url"]) == url_key(url) for other in others):
        match = next(other for other in others if url_key(other["website_url"]) == url_key(url))
        errors.append(f"Another charity already uses this website ({match['org_id'] or match['org_name']}).")
    if values["org_id"] and any(other["org_id"] == values["org_id"] for other in others):
        errors.append(f"Organization ID {values['org_id']} is already used.")
    if usable_value(values["org_email"]) and not EMAIL_RE.fullmatch(values["org_email"]):
        errors.append("Email does not look like an email address.")
    if usable_value(values["org_phone"]) and not any(map(phone_key, split_phone_values(values["org_phone"]))):
        errors.append("Phone does not look like a US phone number.")
    if values["state"] and values["state"].upper() not in US_STATES:
        errors.append("State should be a two-letter abbreviation, e.g. SC.")
    if usable_value(values["zip"]) and not re.fullmatch(r"\d{5}(-\d{4})?", values["zip"]):
        errors.append("ZIP code should be 5 digits (or ZIP+4).")
    missing = [label for key, label, group in FORM_FIELDS if group == "Recommended" and not values[key]]
    if missing:
        warnings.append("Recommended but blank: " + ", ".join(missing) + ".")
    return errors, warnings


def load_targets(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("organizations") or payload.get("targets") or payload.get("records")
    if not isinstance(payload, list):
        raise ValueError("The file should contain a list of charities.")
    return [{field: source_item(record)[field] or "" for field in TARGET_FIELDS} for record in payload]


def free_memory_gb() -> float | None:
    try:
        import psutil  # installed with Botasaurus
    except ImportError:
        return None
    return psutil.virtual_memory().available / 1024 ** 3


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class ScrollFrame(ttk.Frame):
    """A frame whose contents scroll when the window is too short (e.g. laptops)."""

    def __init__(self, master: tk.Misc, fit_width: bool = True, **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scroll.set,
                              background=ttk.Style().lookup("TFrame", "background"))
        # fit_width: ask for the contents' full width (a fixed side column);
        # otherwise take whatever width the window gives (a resizable pane).
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all"),
            **({"width": self.inner.winfo_reqwidth()} if fit_width else {})))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # Scroll with the mouse wheel only while the pointer is over this frame.
        self.bind("<Enter>", lambda _e: self.bind_all_wheel(True))
        self.bind("<Leave>", lambda _e: self.bind_all_wheel(False))

    def bind_all_wheel(self, on: bool) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            if on:
                self.bind_all(sequence, self.on_wheel)
            else:
                self.unbind_all(sequence)

    def on_wheel(self, event: tk.Event) -> None:
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        if event.num == 4 or event.delta > 0:
            self.canvas.yview_scroll(-1, "units")
        else:
            self.canvas.yview_scroll(1, "units")


# --- Database spreadsheet helpers ----------------------------------------------
#
# Power Automate turns charity_contacts_live.json into an Excel spreadsheet in
# this folder.  The Database tab only reads it and writes the cells a person
# edits; scraped data reaches it only through Power Automate.

DATABASE_FIELDS = (
    "org_id", "org_name", "category", "org_description", "website_url", "org_phone",
    "org_email", "street_address", "city", "state", "zip", "is_active", "last_checked",
)
DATABASE_LABELS = {key: label for key, label, _ in FORM_FIELDS} | {
    "is_active": "Active status", "last_checked": "Last checked"}
# org_id identifies the row; the other two are set by every scrape.
READ_ONLY_DATABASE_FIELDS = ("org_id", "is_active", "last_checked")


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def find_charity_table(workbook: Any) -> tuple[Any, int, int, dict[str, int]]:
    """(sheet, header row, last row, {column name: column number}) of the charity table."""
    from openpyxl.utils import range_boundaries

    for sheet in workbook.worksheets:
        refs = [sheet.tables[name].ref for name in sheet.tables] or [sheet.dimensions]
        for ref in refs:
            first_col, header_row, last_col, last_row = range_boundaries(ref)
            columns = {cell_text(sheet.cell(header_row, col).value): col for col in range(first_col, last_col + 1)}
            if "org_id" in columns and "website_url" in columns:
                return sheet, header_row, last_row, columns
    raise ValueError("No sheet has org_id and website_url columns.")


def read_database(path: Path) -> list[dict[str, str]]:
    from openpyxl import load_workbook

    sheet, header_row, last_row, columns = find_charity_table(load_workbook(path, data_only=True))
    rows = []
    for row in range(header_row + 1, last_row + 1):
        record = {field: cell_text(sheet.cell(row, columns[field]).value) if field in columns else ""
                  for field in DATABASE_FIELDS}
        if any(record.values()):
            rows.append(record)
    return rows


def find_spreadsheet(folder: Path) -> Path | None:
    """The newest .xlsx in the folder that has the charity columns."""
    candidates = sorted((path for path in folder.glob("*.xlsx") if not path.name.startswith(("~$", "."))),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            read_database(path)
            return path
        except Exception:  # not the charity spreadsheet, or unreadable
            continue
    return None


def write_database_changes(path: Path, org_id: str, changes: dict[str, str]) -> None:
    """Write only the edited cells of one charity, re-reading the file first.

    Re-reading keeps anything Power Automate wrote since the tab was loaded.
    The new file replaces the old one only once it is completely written.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(path)
    sheet, header_row, last_row, columns = find_charity_table(workbook)
    row = next((row for row in range(header_row + 1, last_row + 1)
                if cell_text(sheet.cell(row, columns["org_id"]).value) == org_id), None)
    if row is None:
        raise ValueError(f"{org_id} is no longer in the spreadsheet. Reload it and try again.")
    for field, value in changes.items():
        if field in columns:
            sheet.cell(row, columns[field]).value = value
    descriptor, temporary = tempfile.mkstemp(prefix=".charity_db_", suffix=".xlsx", dir=path.parent)
    os.close(descriptor)
    try:
        workbook.save(temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# --- Charities tab ------------------------------------------------------------

class CharitiesTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "CharityApp") -> None:
        super().__init__(master, padding=10)
        self.app = app
        self.path: Path | None = None
        self.records: list[dict[str, str]] = []
        self.selected: int | None = None  # index in records, None for a new charity
        self.dirty = False
        self.loading_form = False
        self.form_dirty = False

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open list...", command=self.open_file).pack(side="left")
        ttk.Button(bar, text="Save list", command=self.save).pack(side="left", padx=4)
        ttk.Button(bar, text="Save list as...", command=self.save_as).pack(side="left")
        self.file_label = ttk.Label(bar, text="No file open")
        self.file_label.pack(side="left", padx=12)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(10, 0))
        body.add(self.build_list(body), weight=1)
        right = ttk.Frame(body)
        # The fields scroll on short screens; the buttons below always show.
        form = ScrollFrame(right, fit_width=False)
        actions = ttk.Frame(right, padding=(10, 6, 10, 0))
        actions.pack(side="bottom", fill="x")
        form.pack(side="top", fill="both", expand=True)
        self.build_form(form.inner, actions)
        body.add(right, weight=1)

    # List of charities ---------------------------------------------------
    def build_list(self, master: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(master, padding=(0, 0, 10, 0))
        search = ttk.Frame(frame)
        search.pack(fill="x")
        ttk.Label(search, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        ttk.Entry(search, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=(6, 0))

        columns = ("org_id", "org_name", "category")
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for column, title, width in zip(columns, ("ID", "Name", "Category"), (64, 240, 180)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column != "org_id")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.count_label = ttk.Label(frame, text="")
        self.count_label.pack(anchor="w", pady=(6, 0))
        return frame

    def refresh_list(self, select: int | None = None) -> None:
        term = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for index, record in enumerate(self.records):
            text = " ".join(record.values()).lower()
            if term and term not in text:
                continue
            shown += 1
            self.tree.insert("", "end", iid=str(index),
                             values=(record["org_id"], record["org_name"] or record["website_url"], record["category"]))
        total = len(self.records)
        self.count_label.configure(text=f"{total} charities" + (f" ({shown} shown)" if term else ""))
        if select is not None and self.tree.exists(str(select)):
            self.loading_form = True  # selecting programmatically must not prompt
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
            self.loading_form = False

    # Form ------------------------------------------------------------------
    def build_form(self, master: tk.Misc, actions: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(master, padding=(10, 0, 10, 0))
        frame.pack(fill="both", expand=True)
        title_font = tkfont.nametofont("TkDefaultFont").copy()
        title_font.configure(weight="bold", size=title_font.cget("size") + 2)
        self.form_title = ttk.Label(frame, text="New charity", font=title_font)
        self.form_title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.vars: dict[str, tk.StringVar] = {}
        row, group = 1, None
        for key, label, field_group in FORM_FIELDS:
            if field_group != group:
                group = field_group
                heading = {"Required": "Required", "Recommended": "Recommended",
                           "Optional": "Optional (fill in if the client has it)"}[group]
                ttk.Label(frame, text=heading, foreground="#555").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
                row += 1
            text = label + (" *" if group == "Required" else "")
            ttk.Label(frame, text=text).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
            var = tk.StringVar()
            var.trace_add("write", lambda *_: self.mark_form_dirty())
            self.vars[key] = var
            if key == "org_description":
                self.description = tk.Text(frame, height=5, wrap="word", font="TkDefaultFont")
                self.description.grid(row=row, column=1, sticky="ew", pady=2)
                self.description.bind("<<Modified>>", self.on_description_change)
            elif key == "category":
                self.category_box = ttk.Combobox(frame, textvariable=var)
                self.category_box.grid(row=row, column=1, sticky="ew", pady=2)
            else:
                ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2)
            row += 1
            if key in FIELD_HINTS:
                ttk.Label(frame, text=FIELD_HINTS[key], foreground="#777").grid(
                    row=row, column=1, sticky="w")
                row += 1
        frame.columnconfigure(1, weight=1)

        self.message = ttk.Label(actions, text="", foreground="#a15c00", wraplength=480, justify="left")
        self.message.pack(anchor="w")
        buttons = ttk.Frame(actions)
        buttons.pack(anchor="w", pady=(4, 0))
        self.apply_button = ttk.Button(buttons, text="Add to list", command=self.apply_form)
        self.apply_button.pack(side="left")
        ttk.Button(buttons, text="New charity", command=self.new_charity).pack(side="left", padx=4)
        self.delete_button = ttk.Button(buttons, text="Delete", command=self.delete_selected)
        self.delete_button.pack(side="left")
        ttk.Label(actions, text="Changes go into the list right away; use Save list to write the file.",
                  foreground="#777").pack(anchor="w", pady=(4, 0))
        self.new_charity(confirm=False)
        return frame

    def on_description_change(self, _event: Any) -> None:
        if self.description.edit_modified():
            self.description.edit_modified(False)
            self.mark_form_dirty()

    def mark_form_dirty(self) -> None:
        if not self.loading_form:
            self.form_dirty = True

    def form_values(self) -> dict[str, str]:
        values = {key: var.get().strip() for key, var in self.vars.items()}
        values["org_description"] = " ".join(self.description.get("1.0", "end").split())
        values["website_url"] = normalize_url(values["website_url"])
        values["state"] = values["state"].upper()
        return values

    def fill_form(self, record: dict[str, str]) -> None:
        self.loading_form = True
        for key, var in self.vars.items():
            var.set(record.get(key, ""))
        self.description.delete("1.0", "end")
        self.description.insert("1.0", record.get("org_description", ""))
        self.description.edit_modified(False)
        self.loading_form = False
        self.form_dirty = False
        self.message.configure(text="")

    def discard_form_ok(self) -> bool:
        return not self.form_dirty or messagebox.askyesno(
            "Unsaved charity", "Discard the changes in the form?", parent=self)

    def on_select(self, _event: Any) -> None:
        selection = self.tree.selection()
        if self.loading_form or not selection:
            return
        index = int(selection[0])
        if index == self.selected:
            return
        if not self.discard_form_ok():
            self.refresh_list(select=self.selected)
            return
        self.selected = index
        self.fill_form(self.records[index])
        self.form_title.configure(text=f"Editing {self.records[index]['org_id'] or 'charity'}")
        self.apply_button.configure(text="Update charity")
        self.delete_button.state(["!disabled"])

    def new_charity(self, confirm: bool = True) -> None:
        if confirm and not self.discard_form_ok():
            return
        self.selected = None
        self.fill_form({})
        self.tree.selection_remove(self.tree.selection())
        self.form_title.configure(text="New charity")
        self.apply_button.configure(text="Add to list")
        self.delete_button.state(["disabled"])

    def apply_form(self) -> None:
        values = self.form_values()
        others = [record for index, record in enumerate(self.records) if index != self.selected]
        errors, warnings = validate_charity(values, others)
        if errors:
            messagebox.showerror("Please fix", "\n".join(errors), parent=self)
            return
        if not values["org_id"]:
            values["org_id"] = next_org_id(self.records)
        record = {field: values[field] for field in TARGET_FIELDS}
        if self.selected is None:
            self.records.append(record)
            self.selected = len(self.records) - 1
            action = "Added"
        else:
            self.records[self.selected] = record
            action = "Updated"
        self.set_dirty(True)
        self.update_categories()
        self.refresh_list(select=self.selected)
        self.fill_form(record)
        self.form_title.configure(text=f"Editing {record['org_id']}")
        self.apply_button.configure(text="Update charity")
        self.delete_button.state(["!disabled"])
        self.message.configure(text=" ".join([f"{action} {record['org_id']}.", *warnings]))

    def delete_selected(self) -> None:
        if self.selected is None:
            return
        record = self.records[self.selected]
        if not messagebox.askyesno("Delete charity",
                                   f"Remove {record['org_name'] or record['website_url']} from the list?", parent=self):
            return
        del self.records[self.selected]
        self.set_dirty(True)
        self.form_dirty = False
        self.new_charity(confirm=False)
        self.refresh_list()

    def update_categories(self) -> None:
        self.category_box.configure(values=sorted({r["category"] for r in self.records if r["category"]}))

    # File ------------------------------------------------------------------
    def set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        name = self.path.name if self.path else "unsaved list"
        self.file_label.configure(text=f"{name}{' (unsaved changes)' if dirty else ''}")
        self.app.update_title()

    def load(self, path: Path) -> bool:
        try:
            records = load_targets(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not open", f"{path}\n\n{exc}", parent=self)
            return False
        self.path, self.records = path, records
        self.set_dirty(False)
        self.update_categories()
        self.new_charity(confirm=False)
        self.refresh_list()
        self.app.run_tab.target_var.set(str(path))
        return True

    def open_file(self) -> None:
        if not self.confirm_discard_list():
            return
        name = filedialog.askopenfilename(parent=self, title="Open charity list",
                                          initialdir=str(self.path.parent if self.path else HERE),
                                          filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if name:
            self.load(Path(name))

    def save(self) -> bool:
        if self.form_dirty and not messagebox.askyesno(
                "Form not added", "The form has changes that are not in the list yet. "
                "Save the list without them?", parent=self):
            return False
        if self.path is None:
            return self.save_as()
        try:
            write_json(self.path, self.records)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return False
        self.set_dirty(False)
        return True

    def save_as(self) -> bool:
        name = filedialog.asksaveasfilename(parent=self, title="Save charity list as",
                                            defaultextension=".json",
                                            initialdir=str(self.path.parent if self.path else HERE),
                                            filetypes=[("JSON files", "*.json")])
        if not name:
            return False
        self.path = Path(name)
        self.app.run_tab.target_var.set(name)
        return self.save()

    def apply_database_changes(self, org_id: str, changes: dict[str, str]) -> str:
        """Copy Database-tab edits into the charity list and save it.

        Returns a short note for the user.  The scraper prefers the list's
        values (e.g. description), so this keeps the client's edit on the
        next scrape.
        """
        index = next((i for i, record in enumerate(self.records) if record["org_id"] == org_id), None)
        if self.path is None or index is None:
            return "This charity is not in the open charity list, so only the spreadsheet was changed."
        self.records[index] = {**self.records[index], **changes}
        if self.selected == index:
            self.fill_form(self.records[index])
        self.update_categories()
        self.refresh_list(select=self.selected)
        try:
            write_json(self.path, self.records)
        except OSError as exc:
            self.set_dirty(True)
            return f"The charity list could not be saved ({exc}); save it from the Charities tab."
        self.set_dirty(False)
        return f"Also saved to {self.path.name}, so the next scrape keeps it."

    def confirm_discard_list(self) -> bool:
        return not self.dirty or messagebox.askyesno(
            "Unsaved list", "The charity list has unsaved changes. Discard them?", parent=self)


# --- Database tab -------------------------------------------------------------

class DatabaseTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "CharityApp") -> None:
        super().__init__(master, padding=10)
        self.app = app
        self.path: Path | None = None
        self.rows: list[dict[str, str]] = []
        self.selected: int | None = None

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Choose spreadsheet...", command=self.choose_file).pack(side="left")
        ttk.Button(bar, text="Reload", command=self.reload).pack(side="left", padx=4)
        self.file_label = ttk.Label(bar, text="")
        self.file_label.pack(side="left", padx=12)
        ttk.Label(self, foreground="#555", wraplength=1100, justify="left", text=(
            "This is the database spreadsheet Power Automate builds from the scraper's live file. "
            "Edits saved here change the spreadsheet; edits to fields the charity list has (name, "
            "category, description, website, phone, email, address) are saved to the list too, so the "
            "next scrape keeps them. Scraped data only reaches the spreadsheet through Power Automate."
        )).pack(fill="x", pady=(6, 0))

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(8, 0))
        body.add(self.build_list(body), weight=1)
        right = ttk.Frame(body)
        form = ScrollFrame(right, fit_width=False)
        actions = ttk.Frame(right, padding=(10, 6, 10, 0))
        actions.pack(side="bottom", fill="x")
        form.pack(side="top", fill="both", expand=True)
        self.build_form(form.inner, actions)
        body.add(right, weight=1)

    def build_list(self, master: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(master, padding=(0, 0, 10, 0))
        search = ttk.Frame(frame)
        search.pack(fill="x")
        ttk.Label(search, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        ttk.Entry(search, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=(6, 0))
        columns = ("org_id", "org_name", "org_phone", "is_active", "last_checked")
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for column, title, width in zip(columns, ("ID", "Name", "Phone", "Active", "Checked"),
                                        (64, 190, 120, 60, 90)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == "org_name")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.count_label = ttk.Label(frame, text="")
        self.count_label.pack(anchor="w", pady=(6, 0))
        return frame

    def build_form(self, master: tk.Misc, actions: ttk.Frame) -> None:
        frame = ttk.Frame(master, padding=(10, 0, 10, 0))
        frame.pack(fill="both", expand=True)
        self.title_label = ttk.Label(frame, text="Choose a charity on the left")
        self.title_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.vars: dict[str, tk.StringVar] = {}
        for row, field in enumerate(DATABASE_FIELDS, start=1):
            label = DATABASE_LABELS[field] + (" (set by each scrape)" if field in ("is_active", "last_checked") else "")
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
            if field == "org_description":
                self.description = tk.Text(frame, height=6, wrap="word", font="TkDefaultFont")
                self.description.grid(row=row, column=1, sticky="ew", pady=2)
                continue
            var = self.vars[field] = tk.StringVar()
            state = "readonly" if field in READ_ONLY_DATABASE_FIELDS else "normal"
            ttk.Entry(frame, textvariable=var, state=state).grid(row=row, column=1, sticky="ew", pady=2)
        frame.columnconfigure(1, weight=1)
        self.message = ttk.Label(actions, text="", foreground="#a15c00", wraplength=480, justify="left")
        self.message.pack(anchor="w")
        buttons = ttk.Frame(actions)
        buttons.pack(anchor="w", pady=(4, 0))
        self.save_button = ttk.Button(buttons, text="Save changes to database", command=self.save, state="disabled")
        self.save_button.pack(side="left")
        self.undo_button = ttk.Button(buttons, text="Undo changes", command=self.undo, state="disabled")
        self.undo_button.pack(side="left", padx=4)

    # Loading ---------------------------------------------------------------------
    def load(self, path: Path | None) -> None:
        self.path = path
        self.rows, self.selected = [], None
        if path is None:
            self.file_label.configure(text="No database spreadsheet found in this folder. Use Choose spreadsheet.")
        else:
            try:
                self.rows = read_database(path)
                self.file_label.configure(text=f"{path.name} (loaded {datetime.now():%H:%M})")
            except ImportError:
                self.file_label.configure(text="Reading spreadsheets needs openpyxl: pip install -r requirements.txt")
            except Exception as exc:
                self.file_label.configure(text=f"Could not read {path.name}: {exc}")
        self.clear_form()
        self.refresh_list()

    def reload(self) -> None:
        if self.form_changes() and not messagebox.askyesno(
                "Unsaved edits", "Discard your unsaved edits and reload?", parent=self):
            return
        self.load(self.path or find_spreadsheet(HERE))

    def choose_file(self) -> None:
        name = filedialog.askopenfilename(parent=self, title="Choose the database spreadsheet",
                                          initialdir=str(self.path.parent if self.path else HERE),
                                          filetypes=[("Excel workbooks", "*.xlsx")])
        if name:
            self.load(Path(name))

    def refresh_list(self, select: int | None = None) -> None:
        term = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for index, row in enumerate(self.rows):
            if term and term not in " ".join(row.values()).lower():
                continue
            shown += 1
            self.tree.insert("", "end", iid=str(index), values=tuple(
                row[field] for field in ("org_id", "org_name", "org_phone", "is_active", "last_checked")))
        self.count_label.configure(text=f"{len(self.rows)} charities" + (f" ({shown} shown)" if term else ""))
        if select is not None and self.tree.exists(str(select)):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    # Form ----------------------------------------------------------------------
    def clear_form(self) -> None:
        for var in self.vars.values():
            var.set("")
        self.description.delete("1.0", "end")
        self.title_label.configure(text="Choose a charity on the left")
        self.message.configure(text="")
        self.save_button.state(["disabled"])
        self.undo_button.state(["disabled"])

    def show(self, row: dict[str, str]) -> None:
        for field, var in self.vars.items():
            var.set(row[field])
        self.description.delete("1.0", "end")
        self.description.insert("1.0", row["org_description"])
        self.title_label.configure(text=f"Editing {row['org_id']} in the database")
        in_list = any(r["org_id"] == row["org_id"] for r in self.app.charities_tab.records)
        self.message.configure(text="" if in_list else
                               "This charity is not in the charity list; edits go only to the spreadsheet.")
        self.save_button.state(["!disabled"])
        self.undo_button.state(["!disabled"])

    def form_values(self) -> dict[str, str]:
        values = {field: var.get().strip() for field, var in self.vars.items()}
        values["org_description"] = " ".join(self.description.get("1.0", "end").split())
        return values

    def form_changes(self) -> dict[str, str]:
        if self.selected is None:
            return {}
        original, values = self.rows[self.selected], self.form_values()
        return {field: values[field] for field in DATABASE_FIELDS
                if field not in READ_ONLY_DATABASE_FIELDS and values[field] != original[field]}

    def on_select(self, _event: Any) -> None:
        selection = self.tree.selection()
        if not selection or int(selection[0]) == self.selected:
            return
        if self.form_changes() and not messagebox.askyesno(
                "Unsaved edits", "Discard your unsaved edits to this charity?", parent=self):
            self.refresh_list(select=self.selected)
            return
        self.selected = int(selection[0])
        self.show(self.rows[self.selected])

    def undo(self) -> None:
        if self.selected is not None:
            self.show(self.rows[self.selected])

    def save(self) -> None:
        changes = self.form_changes()
        if not changes:
            self.message.configure(text="Nothing has changed.")
            return
        row = self.rows[self.selected]
        if "website_url" in changes:
            changes["website_url"] = normalize_url(changes["website_url"])
        if "state" in changes:
            changes["state"] = changes["state"].upper()
        charities = self.app.charities_tab
        list_changes = {field: value for field, value in changes.items() if field in TARGET_FIELDS}
        in_list = list_changes and any(r["org_id"] == row["org_id"] for r in charities.records)
        if in_list:
            current = next(r for r in charities.records if r["org_id"] == row["org_id"])
            others = [r for r in charities.records if r["org_id"] != row["org_id"]]
            errors, _ = validate_charity({**current, **list_changes}, others)
            if errors:
                messagebox.showerror("Please fix", "\n".join(errors), parent=self)
                return
            if charities.dirty and not messagebox.askyesno(
                    "Unsaved charity list", "The Charities tab has unsaved changes. Saving this edit "
                    "saves the charity list too, including those changes. Continue?", parent=self):
                return
        try:
            write_database_changes(self.path, row["org_id"], changes)
        except PermissionError:
            messagebox.showerror("Spreadsheet is in use", "Could not save because the spreadsheet is open "
                                 "or still syncing. Close it in Excel, wait for OneDrive, and try again.", parent=self)
            return
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        self.rows[self.selected] = {**row, **changes}
        note = charities.apply_database_changes(row["org_id"], list_changes) if in_list else (
            "Only the spreadsheet was changed." if list_changes else "")
        self.refresh_list(select=self.selected)
        self.show(self.rows[self.selected])
        fields = ", ".join(DATABASE_LABELS[field] for field in changes)
        self.message.configure(text=f"Saved {row['org_id']} ({fields}) to {self.path.name}. {note}".strip())


# --- Run tab --------------------------------------------------------------

class RunTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "CharityApp") -> None:
        super().__init__(master, padding=10)
        self.app = app
        self.process: subprocess.Popen | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.report_path: Path | None = None
        self.output_path: Path | None = None
        self.stopping = False

        options = ScrollFrame(self)
        options.pack(side="left", fill="y", padx=(0, 12))
        self.build_options(options.inner)
        output = ttk.Frame(self)
        output.pack(side="left", fill="both", expand=True)
        self.build_output(output)
        self.update_command()

    def build_options(self, frame: ttk.Frame) -> None:
        self.target_var = tk.StringVar(value=str(DEFAULT_TARGETS))
        self.save_mode = tk.StringVar(value="onedrive")
        self.onedrive_var = tk.StringVar()
        self.local_output_var = tk.StringVar(value=str(HERE / "charity_contacts_live.json"))
        self.local_metadata_var = tk.StringVar(value=str(HERE / "Charity_Contacts_metadata.json"))
        self.keep_backups = tk.StringVar(value="2")
        self.test_run = tk.BooleanVar(value=False)
        self.limit_var = tk.StringVar(value="3")
        self.workers_var = tk.StringVar(value="4")
        self.fetch_mode = tk.StringVar(value="browser")
        self.advanced = {
            "--max-per-contact-type": tk.StringVar(value="5"),
            "--max-contact-pages": tk.StringVar(value="2"),
            "--max-profile-pages": tk.StringVar(value="2"),
            "--delay-seconds": tk.StringVar(value="1.0"),
            "--page-timeout": tk.StringVar(value="30"),
            "--http-workers": tk.StringVar(value="16"),
        }
        self.show_browser = tk.BooleanVar(value=False)
        self.chrome_path = tk.StringVar()

        group = ttk.LabelFrame(frame, text="Charity list", padding=8)
        group.pack(fill="x")
        self.path_row(group, self.target_var, self.browse_target)
        ttk.Checkbutton(group, text="Test run: only the first", variable=self.test_run,
                        command=self.on_test_toggle).grid(row=1, column=0, sticky="w", pady=(6, 0))
        limit = ttk.Frame(group)
        limit.grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Spinbox(limit, from_=1, to=999, width=5, textvariable=self.limit_var).pack(side="left")
        ttk.Label(limit, text="charities").pack(side="left", padx=4)

        group = ttk.LabelFrame(frame, text="Save results to", padding=8)
        group.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(group, text="OneDrive backup (adds a change report; full runs update the live file)",
                        variable=self.save_mode, value="onedrive",
                        command=self.update_command).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(group, text="OneDrive folder (blank = detect automatically):").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=(20, 0))
        self.path_row(group, self.onedrive_var, self.browse_onedrive, row=2, indent=20)
        self.onedrive_note = ttk.Label(group, text="", foreground="#777", wraplength=380)
        self.onedrive_note.grid(row=3, column=0, columnspan=2, sticky="w", padx=(20, 0))
        keep = ttk.Frame(group)
        keep.grid(row=4, column=0, columnspan=2, sticky="w", padx=(20, 0), pady=(4, 0))
        ttk.Label(keep, text="Backups to keep:").pack(side="left")
        ttk.Spinbox(keep, from_=1, to=50, width=4, textvariable=self.keep_backups).pack(side="left", padx=4)
        ttk.Label(keep, text="(older ones are deleted)", foreground="#777").pack(side="left")
        ttk.Radiobutton(group, text="Local files (no backup or report; good for tests)",
                        variable=self.save_mode, value="local",
                        command=self.update_command).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(group, text="Main file:").grid(row=6, column=0, columnspan=2, sticky="w", padx=(20, 0))
        self.path_row(group, self.local_output_var, lambda: self.browse_save(self.local_output_var), row=7, indent=20)
        ttk.Label(group, text="Metadata file:").grid(row=8, column=0, columnspan=2, sticky="w", padx=(20, 0))
        self.path_row(group, self.local_metadata_var, lambda: self.browse_save(self.local_metadata_var),
                      row=9, indent=20)

        group = ttk.LabelFrame(frame, text="Speed", padding=8)
        group.pack(fill="x", pady=(8, 0))
        ttk.Label(group, text="Browsers at once:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(group, from_=1, to=16, width=5, textvariable=self.workers_var).grid(row=0, column=1, sticky="w")
        self.memory_note = ttk.Label(group, text="", wraplength=380, justify="left")
        self.memory_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))
        ttk.Radiobutton(group, text="Load every page in Chrome (most complete)", variable=self.fetch_mode,
                        value="browser", command=self.update_command).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(group, text="Faster: plain web requests first, Chrome only when needed",
                        variable=self.fetch_mode, value="auto",
                        command=self.update_command).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(group, text="The faster mode can miss emails that sites reveal with JavaScript.",
                  foreground="#777", wraplength=380).grid(row=4, column=0, columnspan=2, sticky="w")

        self.show_advanced = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Show advanced options", variable=self.show_advanced,
                        command=self.toggle_advanced).pack(anchor="w", pady=(8, 0))
        group = self.advanced_group = ttk.LabelFrame(frame, text="Advanced", padding=8)
        labels = {
            "--max-per-contact-type": "Best phones/addresses/emails kept (0 = all)",
            "--max-contact-pages": "Contact pages per site",
            "--max-profile-pages": "About/services pages per site",
            "--delay-seconds": "Pause between pages of a site (seconds)",
            "--page-timeout": "Page timeout (seconds)",
            "--http-workers": "Sites at once in faster mode",
        }
        for row, (flag, var) in enumerate(self.advanced.items()):
            ttk.Label(group, text=labels[flag]).grid(row=row, column=0, sticky="w")
            ttk.Entry(group, textvariable=var, width=7).grid(row=row, column=1, sticky="w", padx=(6, 0))
        row = len(self.advanced)
        ttk.Checkbutton(group, text="Show the browser windows", variable=self.show_browser,
                        command=self.update_command).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(group, text="Chrome program (blank = find automatically):").grid(
            row=row + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.path_row(group, self.chrome_path, self.browse_chrome, row=row + 2)

        for var in (self.target_var, self.onedrive_var, self.keep_backups, self.local_output_var, self.local_metadata_var,
                    self.limit_var, self.workers_var, self.chrome_path, *self.advanced.values()):
            var.trace_add("write", lambda *_: self.update_command())

    def toggle_advanced(self) -> None:
        if self.show_advanced.get():
            self.advanced_group.pack(fill="x", pady=(4, 0))
        else:
            self.advanced_group.pack_forget()

    def path_row(self, master: ttk.Frame, var: tk.StringVar, browse, row: int = 0, indent: int = 0) -> None:
        ttk.Entry(master, textvariable=var, width=42).grid(row=row, column=0, sticky="ew", padx=(indent, 0))
        ttk.Button(master, text="Browse...", command=browse).grid(row=row, column=1, sticky="w", padx=(4, 0))
        master.columnconfigure(0, weight=1)

    def build_output(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="Command that will run:").pack(anchor="w")
        self.command_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.command_var, state="readonly").pack(fill="x")

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(8, 4))
        self.run_button = ttk.Button(controls, text="Run scraper", command=self.start)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.report_button = ttk.Button(controls, text="Open change report", state="disabled",
                                        command=lambda: self.report_path and open_path(self.report_path))
        self.report_button.pack(side="left", padx=(12, 0))
        self.folder_button = ttk.Button(controls, text="Open output folder", state="disabled",
                                        command=lambda: self.output_path and open_path(self.output_path.parent))
        self.folder_button.pack(side="left", padx=4)

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.pack(fill="x")
        self.status = ttk.Label(frame, text="Ready.")
        self.status.pack(anchor="w", pady=(4, 4))

        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word", state="disabled", height=20, font="TkFixedFont")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log.tag_configure("problem", foreground="#b00020")
        self.log.tag_configure("good", foreground="#1b5e20")

    # Browse buttons ------------------------------------------------------------
    def browse_target(self) -> None:
        name = filedialog.askopenfilename(parent=self, filetypes=[("JSON files", "*.json")])
        if name:
            self.target_var.set(name)

    def browse_onedrive(self) -> None:
        name = filedialog.askdirectory(parent=self, title="Choose the OneDrive folder for backups")
        if name:
            self.onedrive_var.set(name)

    def browse_save(self, var: tk.StringVar) -> None:
        name = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                            filetypes=[("JSON files", "*.json")])
        if name:
            var.set(name)

    def browse_chrome(self) -> None:
        name = filedialog.askopenfilename(parent=self, title="Choose the Chrome program")
        if name:
            self.chrome_path.set(name)

    # Options ----------------------------------------------------------------------
    def on_test_toggle(self) -> None:
        if self.test_run.get():
            # A test run would otherwise take one of the two OneDrive backups.
            self.save_mode.set("local")
        self.update_command()

    def command(self) -> list[str]:
        args = [sys.executable, "-u", str(SCRAPER), "--input", self.target_var.get().strip()]
        if self.save_mode.get() == "local":
            args += ["--local-output", "--output", self.local_output_var.get().strip(),
                     "--metadata-output", self.local_metadata_var.get().strip()]
        else:
            if self.onedrive_var.get().strip():
                args += ["--onedrive-folder", self.onedrive_var.get().strip()]
            args += ["--keep-backups", self.keep_backups.get().strip()]
        if self.test_run.get():
            args += ["--limit", self.limit_var.get().strip()]
        args += ["--workers", self.workers_var.get().strip(), "--fetch-mode", self.fetch_mode.get()]
        for flag, var in self.advanced.items():
            if flag != "--http-workers" or self.fetch_mode.get() == "auto":
                args += [flag, var.get().strip()]
        if self.show_browser.get():
            args.append("--show-browser")
        if self.chrome_path.get().strip():
            args += ["--chrome-path", self.chrome_path.get().strip()]
        return args

    def update_command(self) -> None:
        shown = ["python", SCRAPER.name, *self.command()[3:]]
        self.command_var.set(subprocess.list2cmdline(shown))
        self.update_memory_note()
        self.update_onedrive_note()

    def update_memory_note(self) -> None:
        try:
            workers = int(self.workers_var.get())
        except ValueError:
            self.memory_note.configure(text="Enter a whole number of browsers.", foreground="#b00020")
            return
        needed = workers * GB_PER_BROWSER
        note = f"Each browser uses about {GB_PER_BROWSER} GB of memory, so {workers} use about {needed:.1f} GB."
        free = free_memory_gb()
        color = "#555"
        if free is not None:
            safe = max(1, int((free - 0.8) / GB_PER_BROWSER))
            note += f" This computer has {free:.1f} GB free, so up to {min(safe, 16)} at once should be fine."
            if workers > safe:
                note += " That is more than recommended; the computer may slow down."
                color = "#a15c00"
        self.memory_note.configure(text=note, foreground=color)

    def update_onedrive_note(self) -> None:
        if self.onedrive_var.get().strip():
            self.onedrive_note.configure(text="Backups will go in the folder above.")
            return
        try:
            folder = onedrive_destination(None)
            self.onedrive_note.configure(text=f"Detected: {folder}")
        except ValueError:
            self.onedrive_note.configure(text="No OneDrive folder found on this computer. Choose one "
                                              "with Browse, or save to local files.")

    def check_options(self) -> list[str]:
        problems = []
        if not Path(self.target_var.get().strip()).is_file():
            problems.append("The charity list file does not exist.")
        numbers = {"Browsers at once": self.workers_var.get(), **{
            flag: var.get() for flag, var in self.advanced.items()}}
        if self.test_run.get():
            numbers["Test run count"] = self.limit_var.get()
        if self.save_mode.get() == "onedrive":
            numbers["Backups to keep"] = self.keep_backups.get()
        for label, value in numbers.items():
            try:
                if float(value) < 0:
                    raise ValueError
            except ValueError:
                problems.append(f"{label.lstrip('-')} must be a number.")
        if self.save_mode.get() == "onedrive" and not self.onedrive_var.get().strip():
            try:
                onedrive_destination(None)
            except ValueError:
                problems.append("No OneDrive folder was found. Choose one, or save to local files.")
        return problems

    # Running -------------------------------------------------------------------------
    def start(self) -> None:
        charities = self.app.charities_tab
        target = Path(self.target_var.get().strip())
        if charities.dirty and charities.path and target.resolve() == charities.path.resolve():
            answer = messagebox.askyesnocancel(
                "Unsaved list", "The charity list has unsaved changes. Save them before running?", parent=self)
            if answer is None or (answer and not charities.save()):
                return
        problems = self.check_options()
        if problems:
            messagebox.showerror("Cannot run yet", "\n".join(problems), parent=self)
            return
        self.clear_log()
        self.report_path = self.output_path = None
        self.report_button.state(["disabled"])
        self.folder_button.state(["disabled"])
        self.progress.configure(value=0, maximum=1)
        self.stopping = False
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True  # lets Stop end Chrome too
        try:
            self.process = subprocess.Popen(
                self.command(), cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", **options)
        except OSError as exc:
            messagebox.showerror("Could not start", str(exc), parent=self)
            return
        self.write_log(f"Started: {self.command_var.get()}\n")
        self.status.configure(text="Running...")
        self.run_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        threading.Thread(target=self.read_output, args=(self.process,), daemon=True).start()
        self.after(100, self.poll_output)

    def read_output(self, process: subprocess.Popen) -> None:
        for line in process.stdout:  # type: ignore[union-attr]
            self.lines.put(line)
        process.wait()
        self.lines.put(None)

    def poll_output(self) -> None:
        finished = False
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                finished = True
                break
            self.handle_line(line)
        if finished:
            self.finish()
        else:
            self.after(100, self.poll_output)

    def handle_line(self, line: str) -> None:
        progress = re.match(r"\[(\d+)/(\d+)\]\s+(\S+)\s+(\S+)", line)
        if progress:
            done, total = int(progress[1]), int(progress[2])
            self.progress.configure(value=done, maximum=total)
            self.status.configure(text=f"Running... {done} of {total} charities checked")
        report = re.match(r"Wrote change report to (.+)$", line.strip())
        if report:
            self.report_path = Path(report[1])
        written = re.match(r"Wrote \d+ (?:live scrape|source inventory) records to (.+?) in \d+s$", line.strip())
        if written:
            self.output_path = Path(written[1])
        problem = re.search(r"Traceback|Error|failed|Could not|WARNING", line)
        tag = "problem" if problem else "good" if line.startswith("Wrote") else None
        self.write_log(line, tag)

    def finish(self) -> None:
        code = self.process.returncode if self.process else None
        self.process = None
        self.run_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        if self.stopping:
            self.status.configure(text="Stopped. Nothing new was saved for this run.")
        elif code == 0:
            self.progress.configure(value=self.progress["maximum"])
            self.status.configure(text="Finished.")
        else:
            self.status.configure(text=f"The scraper stopped with an error (exit code {code}). See the log.")
        if self.report_path and self.report_path.exists():
            self.report_button.state(["!disabled"])
        if self.output_path and self.output_path.exists():
            self.folder_button.state(["!disabled"])

    def stop(self) -> None:
        if not self.process or not messagebox.askyesno(
                "Stop scraper", "Stop the run? Results from this run will not be saved.", parent=self):
            return
        self.stopping = True
        self.status.configure(text="Stopping...")
        self.kill_process()

    def kill_process(self) -> None:
        if not self.process:
            return
        try:
            if sys.platform == "win32":
                # /T also ends the Chrome browsers the scraper started.
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def write_log(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")


# --- Window -----------------------------------------------------------------

class CharityApp(tk.Tk):
    def __init__(self, targets: Path | None = None) -> None:
        super().__init__()
        self.geometry("1180x700")
        self.minsize(960, 560)
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        self.run_tab = RunTab(notebook, self)
        self.charities_tab = CharitiesTab(notebook, self)
        self.database_tab = DatabaseTab(notebook, self)
        notebook.add(self.charities_tab, text="Charities")
        notebook.add(self.database_tab, text="Database")
        notebook.add(self.run_tab, text="Run")
        self.notebook = notebook
        self.update_title()
        path = targets or DEFAULT_TARGETS
        if path.exists():
            self.charities_tab.load(path)
        self.database_tab.load(find_spreadsheet(HERE))
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def update_title(self) -> None:
        tab = getattr(self, "charities_tab", None)
        star = "*" if tab and tab.dirty else ""
        self.title(f"Charity Contacts{star}")

    def on_close(self) -> None:
        if self.run_tab.process and not messagebox.askyesno(
                "Scraper running", "The scraper is still running. Stop it and close?", parent=self):
            return
        if self.database_tab.form_changes() and not messagebox.askyesno(
                "Unsaved database edits", "Discard your unsaved database edits?", parent=self):
            return
        if not self.charities_tab.confirm_discard_list():
            return
        self.run_tab.stopping = True
        self.run_tab.kill_process()
        self.destroy()


def main() -> None:
    targets = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    CharityApp(targets).mainloop()


if __name__ == "__main__":
    main()
