"""Restaurant inventory ledger backed by Google Sheets.

Reads use the spreadsheet's CSV export. gspread is intentionally reserved for writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import quote
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Restaurant Inventory", page_icon="📦", layout="wide")

MASTER_HEADERS = ["Item Name", "Shortcode", "Category", "Unit", "Par Level"]
LEDGER_HEADERS = [
    "Date", "Timestamp", "Shortcode", "Opening Stock",
    "Purchases", "Closing Stock", "Variance",
]
AUDIT_HEADERS = [
    "Timestamp", "Date", "Shortcode", "Field",
    "Old Value", "New Value", "Reason",
]
SHEET_HEADERS = {
    "Master_Items": MASTER_HEADERS,
    "Daily_Ledger": LEDGER_HEADERS,
    "Audit_Log": AUDIT_HEADERS,
}


def secret(name: str, default=None):
    return st.secrets[name] if name in st.secrets else default


@st.cache_resource(show_spinner=False)
def spreadsheet():
    """Create the gspread client used only to append or update worksheet values."""
    info = dict(st.secrets["google_service_account"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(credentials).open_by_key(st.secrets["spreadsheet_id"])


@st.cache_resource(show_spinner=False)
def get_write_sheets():
    """Return worksheet handles for writes; sheets are initialised on first run."""
    book = spreadsheet()
    sheets = {}
    for name, headers in SHEET_HEADERS.items():
        try:
            sheets[name] = book.worksheet(name)
        except gspread.WorksheetNotFound:
            sheet = book.add_worksheet(title=name, rows=2000, cols=len(headers))
            sheet.append_row(headers, value_input_option="USER_ENTERED")
            sheets[name] = sheet
    return sheets


@st.cache_data(ttl=60, show_spinner=False)
def load_sheet_data(sheet_name: str) -> pd.DataFrame:
    """Load one sheet via its CSV export without using Google Sheets API read quota."""
    spreadsheet_id = st.secrets["spreadsheet_id"]
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?"
        f"tqx=out:csv&sheet={quote(sheet_name, safe='')}"
    )
    try:
        return pd.read_csv(csv_url, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=SHEET_HEADERS[sheet_name])


def as_number(value) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def operational_date() -> str:
    now = datetime.now(ZoneInfo(secret("timezone", "Asia/Kolkata")))
    if now.hour < 4:
        now -= timedelta(days=1)
    return now.date().isoformat()


def timestamp() -> str:
    return datetime.now(ZoneInfo(secret("timezone", "Asia/Kolkata"))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def existing_row(ledger: pd.DataFrame, day: str, shortcode: str) -> int | None:
    matches = ledger.index[
        (ledger["Date"].astype(str) == day)
        & (ledger["Shortcode"].astype(str) == shortcode)
    ]
    return int(matches[0]) if len(matches) else None


def opening_for(ledger: pd.DataFrame, day: str, shortcode: str) -> float:
    if ledger.empty:
        return 0.0
    prior = ledger[
        (ledger["Shortcode"].astype(str) == shortcode)
        & (ledger["Date"].astype(str) < day)
    ]
    if prior.empty:
        return 0.0
    return as_number(prior.sort_values("Date").iloc[-1]["Closing Stock"])


def save_entry(
    ledger_sheet,
    ledger: pd.DataFrame,
    day: str,
    shortcode: str,
    purchases: float,
    closing: float,
) -> tuple[float, float]:
    """Append or update one operational-day ledger entry."""
    prior_index = existing_row(ledger, day, shortcode)
    opening = opening_for(ledger, day, shortcode)
    if prior_index is not None:
        existing = ledger.loc[prior_index]
        opening = as_number(existing["Opening Stock"])
        if purchases is None:
            purchases = as_number(existing["Purchases"])
        if closing is None:
            closing = as_number(existing["Closing Stock"])

    purchases = as_number(purchases)
    closing = as_number(closing)
    variance = closing - (opening + purchases)
    values = [day, timestamp(), shortcode, opening, purchases, closing, variance]

    if prior_index is None:
        ledger_sheet.append_row(values, value_input_option="USER_ENTERED")
    else:
        ledger_sheet.update(
            values=[values],
            range_name=f"A{prior_index + 2}:G{prior_index + 2}",
            value_input_option="USER_ENTERED",
        )
    return opening, variance


def finish_write(message: str) -> None:
    """Invalidate cached CSV reads only after a successful gspread write."""
    st.session_state["write_success"] = message
    st.cache_data.clear()
    st.rerun()


def item_picker(items: list[dict], key: str) -> dict | None:
    query = st.text_input(
        "Search item (shortcode or name)",
        key=f"search_{key}",
        placeholder="e.g. TOM or Tomato",
    ).strip().lower()
    matches = [
        item for item in items
        if not query
        or query in str(item.get("Shortcode", "")).lower()
        or query in str(item.get("Item Name", "")).lower()
    ]
    if not matches:
        st.warning("No matching item. Add it below.")
        return None
    labels = {
        f"{item['Shortcode']} — {item['Item Name']} ({item['Unit']})": item
        for item in matches
    }
    label = st.selectbox("Choose item", list(labels), key=f"choice_{key}")
    return labels[label]


def remembered_options(items: list[dict], field: str, defaults: list[str]) -> list[str]:
    """Return a stable, de-duplicated dropdown list from Master Items."""
    remembered = [
        str(item.get(field, "")).strip()
        for item in items
        if str(item.get(field, "")).strip()
    ]
    return list(dict.fromkeys([*defaults, *sorted(remembered, key=str.lower)]))


def manager_view(master_sheet, ledger_sheet, audit_sheet) -> None:
    st.title("📦 Daily Inventory")
    day = operational_date()
    st.caption(
        f"Operational date: **{day}** · Entries before 4:00 AM belong to the prior day."
    )
    master_df = load_sheet_data("Master_Items")
    items = master_df.to_dict("records")
    ledger = load_sheet_data("Daily_Ledger")

    purchases_tab, closing_tab, add_tab, bulk_tab, edit_tab, correction_tab = st.tabs(
        [
            "1. Daily Purchases",
            "2. Closing Stock",
            "Add New Item",
            "Bulk Upload Master Items",
            "Edit Master Items",
            "Correct Entry",
        ]
    )

    with purchases_tab:
        item = item_picker(items, "purchase") if items else None
        if item:
            with st.form("purchase_form", clear_on_submit=True):
                quantity = st.number_input(
                    f"Purchase quantity ({item['Unit']})",
                    min_value=0.0,
                    step=0.1,
                    format="%.2f",
                )
                submitted = st.form_submit_button(
                    "Save Purchase", type="primary", use_container_width=True
                )
            if submitted:
                index = existing_row(ledger, day, item["Shortcode"])
                existing_close = (
                    as_number(ledger.loc[index, "Closing Stock"])
                    if index is not None else 0.0
                )
                opening, _ = save_entry(
                    ledger_sheet, ledger, day, item["Shortcode"], quantity, existing_close
                )
                finish_write(f"Purchase saved. Opening stock: {opening:g} {item['Unit']}.")

    with closing_tab:
        item = item_picker(items, "closing") if items else None
        if item:
            with st.form("closing_form", clear_on_submit=True):
                quantity = st.number_input(
                    f"Physical closing stock ({item['Unit']})",
                    min_value=0.0,
                    step=0.1,
                    format="%.2f",
                )
                submitted = st.form_submit_button(
                    "Save Closing Stock", type="primary", use_container_width=True
                )
            if submitted:
                index = existing_row(ledger, day, item["Shortcode"])
                existing_purchase = (
                    as_number(ledger.loc[index, "Purchases"])
                    if index is not None else 0.0
                )
                opening, variance = save_entry(
                    ledger_sheet, ledger, day, item["Shortcode"], existing_purchase, quantity
                )
                finish_write(
                    f"Closing stock saved. Expected: {opening + existing_purchase:g}; "
                    f"variance: {variance:g} {item['Unit']}."
                )

    with add_tab:
        st.caption(
            "Categories and units saved with an item are remembered and offered for future items."
        )
        unit_options = remembered_options(items, "Unit", ["kg", "L", "pcs"])
        category_options = remembered_options(items, "Category", [])
        new_unit_option = "+ Add New Unit"
        new_category_option = "+ Add New Category"

        selected_unit = st.selectbox(
            "Standard unit",
            [*unit_options, new_unit_option],
            key="new_item_unit",
        )
        custom_unit = (
            st.text_input(
                "New unit name",
                placeholder="e.g. carton, packet, tray",
                key="new_item_custom_unit",
            ).strip()
            if selected_unit == new_unit_option
            else ""
        )
        unit = custom_unit if selected_unit == new_unit_option else selected_unit

        selected_category = st.selectbox(
            "Category",
            [*category_options, new_category_option],
            key="new_item_category",
        )
        custom_category = (
            st.text_input(
                "New category name",
                placeholder="e.g. Dairy",
                key="new_item_custom_category",
            ).strip()
            if selected_category == new_category_option
            else ""
        )
        category = (
            custom_category if selected_category == new_category_option else selected_category
        )

        with st.form("new_item_form", clear_on_submit=True):
            name = st.text_input("Item name", placeholder="Tomato")
            shortcode = st.text_input("Shortcode", placeholder="TOM").strip().upper()
            added = st.form_submit_button(
                "Add Item", type="primary", use_container_width=True
            )
        if added:
            if not name or not shortcode:
                st.error("Please enter both an item name and shortcode.")
            elif not category or not unit:
                st.error("Please select or add both a category and unit.")
            elif any(str(item["Shortcode"]).upper() == shortcode for item in items):
                st.error("That shortcode is already in use.")
            elif any(str(item["Item Name"]).casefold() == name.strip().casefold() for item in items):
                st.error("That item name already exists.")
            else:
                master_sheet.append_row(
                    [name.strip(), shortcode, category, unit, 0],
                    value_input_option="USER_ENTERED",
                )
                finish_write(
                    f"{name.strip()} was added. Its category and unit are now reusable."
                )

    with bulk_tab:
        st.caption("Upload an Excel file with columns: Name, Category, Unit.")
        upload = st.file_uploader(
            "Master-items Excel file",
            type=["xlsx", "xls"],
            key="bulk_master_items_upload",
        )
        if upload is not None and st.button(
            "Import Missing Master Items", type="primary", use_container_width=True
        ):
            try:
                uploaded_df = pd.read_excel(upload, dtype=str).fillna("")
            except Exception as exc:
                st.error(f"Could not read that Excel file: {exc}")
            else:
                source_columns = {
                    str(column).strip().casefold(): column for column in uploaded_df.columns
                }
                required_columns = {"name", "category", "unit"}
                missing_columns = required_columns - set(source_columns)
                if missing_columns:
                    st.error(
                        "Missing required column(s): "
                        + ", ".join(sorted(column.title() for column in missing_columns))
                    )
                else:
                    existing_names = {
                        str(item.get("Item Name", "")).strip().casefold() for item in items
                    }
                    rows_to_add = []
                    skipped = 0
                    for _, uploaded in uploaded_df.iterrows():
                        name = str(uploaded[source_columns["name"]]).strip()
                        category = str(uploaded[source_columns["category"]]).strip()
                        unit = str(uploaded[source_columns["unit"]]).strip()
                        normalized_name = name.casefold()
                        if not name or not category or not unit:
                            skipped += 1
                            continue
                        if normalized_name in existing_names:
                            skipped += 1
                            continue
                        rows_to_add.append([name, name[:4].upper(), category, unit, 0])
                        existing_names.add(normalized_name)

                    if not rows_to_add:
                        st.info("No new complete items were found. Existing names were skipped.")
                    else:
                        for row in rows_to_add:
                            master_sheet.append_row(row, value_input_option="USER_ENTERED")
                        finish_write(
                            f"Imported {len(rows_to_add)} item(s). "
                            f"Skipped {skipped} blank or duplicate row(s). "
                            "Their categories and units are now available in all item forms."
                        )

    with edit_tab:
        if master_df.empty:
            st.info("Add a master item before editing.")
        else:
            edit_labels = {
                f"{record['Shortcode']} — {record['Item Name']}": record
                for record in items
            }
            selected_label = st.selectbox(
                "Select master item", list(edit_labels), key="edit_master_item"
            )
            selected = edit_labels[selected_label]
            current_unit_options = remembered_options(items, "Unit", ["kg", "L", "pcs"])
            current_category_options = remembered_options(items, "Category", [])
            edit_unit_option = "+ Add New Unit"
            edit_category_option = "+ Add New Category"

            with st.form("edit_master_item_form"):
                new_name = st.text_input("Item name", value=str(selected["Item Name"]))
                chosen_category = st.selectbox(
                    "Category",
                    [*current_category_options, edit_category_option],
                    index=current_category_options.index(str(selected["Category"]))
                    if str(selected["Category"]) in current_category_options else 0,
                )
                new_category = st.text_input(
                    "New category name",
                    key="edit_custom_category",
                ).strip() if chosen_category == edit_category_option else chosen_category
                chosen_unit = st.selectbox(
                    "Unit",
                    [*current_unit_options, edit_unit_option],
                    index=current_unit_options.index(str(selected["Unit"]))
                    if str(selected["Unit"]) in current_unit_options else 0,
                )
                new_unit = st.text_input(
                    "New unit name",
                    key="edit_custom_unit",
                ).strip() if chosen_unit == edit_unit_option else chosen_unit
                reason = st.text_input(
                    "Reason for change", placeholder="e.g. Corrected category"
                ).strip()
                saved = st.form_submit_button(
                    "Save Master Item Changes", type="primary", use_container_width=True
                )

            if saved:
                if not new_name.strip() or not new_category or not new_unit:
                    st.error("Item name, category, and unit are required.")
                elif not reason:
                    st.error("Please enter a reason for the change.")
                else:
                    matching_rows = master_df.index[
                        master_df["Shortcode"].astype(str) == str(selected["Shortcode"])
                    ]
                    row_number = int(matching_rows[0]) + 2 if len(matching_rows) else None
                    changes = [
                        ("Item Name", str(selected["Item Name"]), new_name.strip()),
                        ("Category", str(selected["Category"]), new_category),
                        ("Unit", str(selected["Unit"]), new_unit),
                    ]
                    changes = [change for change in changes if change[1] != change[2]]
                    if not changes:
                        st.info("No item fields were changed.")
                    elif row_number is None:
                        st.error("The selected item could not be located.")
                    else:
                        master_sheet.update(
                            values=[[
                                new_name.strip(),
                                selected["Shortcode"],
                                new_category,
                                new_unit,
                                selected.get("Par Level", 0),
                            ]],
                            range_name=f"A{row_number}:E{row_number}",
                            value_input_option="USER_ENTERED",
                        )
                        for field, old_value, new_value in changes:
                            audit_sheet.append_row(
                                [
                                    timestamp(),
                                    day,
                                    selected["Shortcode"],
                                    field,
                                    old_value,
                                    new_value,
                                    reason,
                                ],
                                value_input_option="USER_ENTERED",
                            )
                        finish_write(
                            f"Updated {selected['Shortcode']} and recorded {len(changes)} audit change(s)."
                        )

    with correction_tab:
        today_entries = ledger[ledger["Date"].astype(str) == day] if not ledger.empty else ledger
        if today_entries.empty:
            st.info("No entries have been logged for today yet.")
        else:
            codes = today_entries["Shortcode"].astype(str).tolist()
            selected_code = st.selectbox("Select item to correct", codes)
            entry = today_entries[today_entries["Shortcode"].astype(str) == selected_code].iloc[0]
            row_index = int(
                today_entries[today_entries["Shortcode"].astype(str) == selected_code].index[0]
            )
            field = st.radio("Field to correct", ["Purchases", "Closing Stock"])
            old_value = as_number(entry[field])
            new_value = st.number_input(
                f"New {field} quantity", min_value=0.0, value=old_value, step=0.1
            )
            reason = st.text_input("Reason for correction", placeholder="e.g. Typo during entry")
            if st.button("Submit Correction", type="primary"):
                if new_value == old_value:
                    st.warning("The new quantity is the same as the current quantity.")
                elif not reason.strip():
                    st.error("Please provide a reason for the correction.")
                else:
                    purchases = new_value if field == "Purchases" else as_number(entry["Purchases"])
                    closing = new_value if field == "Closing Stock" else as_number(entry["Closing Stock"])
                    opening = as_number(entry["Opening Stock"])
                    variance = closing - (opening + purchases)
                    row = row_index + 2
                    ledger_sheet.update(
                        values=[[purchases, closing, variance]],
                        range_name=f"E{row}:G{row}",
                        value_input_option="USER_ENTERED",
                    )
                    audit_sheet.append_row(
                        [timestamp(), day, selected_code, field, old_value, new_value, reason.strip()],
                        value_input_option="USER_ENTERED",
                    )
                    finish_write(
                        f"{field} corrected from {old_value:g} to {new_value:g}; audit entry saved."
                    )


def owner_view() -> None:
    st.subheader("👑 Owner Dashboard")
    configured_password = secret("owner_password")
    if not configured_password:
        st.warning("Set owner_password in Streamlit secrets to enable Owner View.")
        return
    if not st.session_state.get("owner_authenticated"):
        with st.form("owner_login"):
            password = st.text_input("Owner password", type="password")
            submitted = st.form_submit_button("Open Owner View", type="primary")
        if submitted:
            if password == configured_password:
                st.session_state["owner_authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    ledger = load_sheet_data("Daily_Ledger")
    audit = load_sheet_data("Audit_Log")
    audit_tab, history_tab = st.tabs(["Correction Audit Trail", "History & Export"])

    with audit_tab:
        if audit.empty:
            st.info("No quantity corrections have been made.")
        else:
            for _, row in audit.sort_values("Timestamp", ascending=False).iterrows():
                st.warning(
                    f"**[{row['Timestamp']}]** **{row['Shortcode']}** — {row['Field']} "
                    f"changed from **{row['Old Value']}** to **{row['New Value']}**.  \n"
                    f"Reason: {row['Reason']} · Operational date: {row['Date']}"
                )

    with history_tab:
        if ledger.empty:
            st.info("No ledger entries have been saved yet.")
        else:
            dates = sorted(ledger["Date"].astype(str).unique(), reverse=True)
            selected_dates = st.multiselect("Show operational dates", dates, default=dates[:7])
            shown = ledger[ledger["Date"].astype(str).isin(selected_dates)]
            st.dataframe(shown, use_container_width=True, hide_index=True)
            output = BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                ledger.to_excel(writer, index=False, sheet_name="Daily Ledger")
                audit.to_excel(writer, index=False, sheet_name="Audit Log")
            st.download_button(
                "Download complete master log (.xlsx)",
                output.getvalue(),
                "restaurant_inventory_log.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )

    if st.button("Lock Owner View"):
        st.session_state["owner_authenticated"] = False
        st.rerun()


def main() -> None:
    st.sidebar.title("Restaurant Inventory")
    mode = st.sidebar.radio("Choose view", ["Manager", "Owner"])
    if message := st.session_state.pop("write_success", None):
        st.success(message)

    try:
        sheets = get_write_sheets()
        if mode == "Manager":
            manager_view(
                sheets["Master_Items"],
                sheets["Daily_Ledger"],
                sheets["Audit_Log"],
            )
        else:
            owner_view()
    except HTTPError as exc:
        if exc.code in (401, 403):
            st.error(
                "CSV access is blocked. Share the spreadsheet as Viewer for anyone "
                "with the link, or publish it to the web, then reload this app."
            )
        else:
            st.error(f"Could not read the spreadsheet CSV export (HTTP {exc.code}).")
    except Exception as exc:
        st.error("Could not connect to Google Sheets. Check Streamlit secrets and sheet sharing.")
        st.exception(exc)


if __name__ == "__main__":
    main()
