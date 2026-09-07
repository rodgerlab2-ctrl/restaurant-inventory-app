"""Restaurant inventory ledger backed by Google Sheets."""
from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Restaurant Inventory", page_icon="📦", layout="wide")

MASTER_HEADERS = ["Shortcode", "Item Name", "Category", "Unit"]
LEDGER_HEADERS = [
    "Date", "Shortcode", "Item Name", "Opening Stock", "Purchases",
    "Closing Stock", "Variance", "Timestamp",
]


def secret(name: str, default=None):
    """Safely read optional Streamlit secrets."""
    return st.secrets[name] if name in st.secrets else default


@st.cache_resource(show_spinner=False)
def spreadsheet():
    """Authenticate once per Streamlit process and return the configured workbook."""
    info = dict(st.secrets["google_service_account"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(credentials)
    return client.open_by_key(st.secrets["spreadsheet_id"])


def ensure_worksheets(book):
    """Create missing worksheets and headers. This makes first deployment painless."""
    required = {"Master_Items": MASTER_HEADERS, "Daily_Ledger": LEDGER_HEADERS}
    sheets = {}
    for title, headers in required.items():
        try:
            sheet = book.worksheet(title)
        except gspread.WorksheetNotFound:
            sheet = book.add_worksheet(title=title, rows=2000, cols=len(headers))
        if not sheet.row_values(1):
            sheet.append_row(headers)
            sheet.freeze(rows=1)
        sheets[title] = sheet
    return sheets


def as_number(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def operational_date() -> str:
    timezone = ZoneInfo(secret("timezone", "Asia/Kolkata"))
    now = datetime.now(timezone)
    if now.hour < 4:
        now -= timedelta(days=1)
    return now.date().isoformat()


def load_records(sheet) -> pd.DataFrame:
    records = sheet.get_all_records(expected_headers=LEDGER_HEADERS)
    return pd.DataFrame(records, columns=LEDGER_HEADERS)


def opening_for(ledger: pd.DataFrame, day: str, shortcode: str) -> float:
    """Return the most recent earlier closing quantity for the item, or zero."""
    if ledger.empty:
        return 0.0
    prior = ledger[(ledger["Shortcode"] == shortcode) & (ledger["Date"].astype(str) < day)]
    if prior.empty:
        return 0.0
    latest = prior.sort_values("Date").iloc[-1]
    return as_number(latest["Closing Stock"])


def existing_row(ledger: pd.DataFrame, day: str, shortcode: str):
    matches = ledger.index[(ledger["Date"].astype(str) == day) & (ledger["Shortcode"] == shortcode)]
    return int(matches[0]) if len(matches) else None


def save_entry(sheet, ledger: pd.DataFrame, day: str, item: dict, purchases: float, closing: float):
    """Upsert an item/day entry; repeated saves update instead of creating duplicates."""
    code = item["Shortcode"]
    prior_index = existing_row(ledger, day, code)
    opening = opening_for(ledger, day, code)
    if prior_index is not None:
        old = ledger.loc[prior_index]
        # Keep a previously entered value if this action did not change that field.
        purchases = purchases if purchases is not None else as_number(old["Purchases"])
        closing = closing if closing is not None else as_number(old["Closing Stock"])
        opening = as_number(old["Opening Stock"])
    purchases, closing = as_number(purchases), as_number(closing)
    variance = closing - (opening + purchases)
    values = [day, code, item["Item Name"], opening, purchases, closing, variance,
              datetime.now().isoformat(timespec="seconds")]
    if prior_index is None:
        sheet.append_row(values, value_input_option="USER_ENTERED")
    else:
        # DataFrame index is zero-based; row 1 is headers.
        row_number = prior_index + 2
        sheet.update(f"A{row_number}:H{row_number}", [values], value_input_option="USER_ENTERED")
    return opening, variance


def item_picker(items: list[dict], key: str) -> dict | None:
    query = st.text_input("Search item (shortcode or name)", key=f"search_{key}", placeholder="e.g. TOM or Tomato")
    query = query.strip().lower()
    matches = [i for i in items if not query or query in i["Shortcode"].lower() or query in i["Item Name"].lower()]
    if not matches:
        st.warning("No matching item. Add it below.")
        return None
    labels = {f"{i['Shortcode']} — {i['Item Name']} ({i['Unit']})": i for i in matches}
    return st.selectbox("Choose item", list(labels), key=f"choice_{key}", format_func=str) and labels[st.session_state[f"choice_{key}"]]


def manager_view(master_sheet, ledger_sheet):
    st.title("📦 Daily Inventory")
    day = operational_date()
    st.caption(f"Operational date: **{day}** · Entries before 4:00 AM belong to the prior restaurant day.")
    items = master_sheet.get_all_records(expected_headers=MASTER_HEADERS)
    if not items:
        st.info("Start by adding your first stock item below.")

    purchase_tab, closing_tab, new_item_tab = st.tabs(["1. Daily Purchases", "2. Closing Stock", "Add New Item"])
    with purchase_tab:
        item = item_picker(items, "purchase") if items else None
        if item:
            with st.form("purchase_form", clear_on_submit=True):
                qty = st.number_input(f"Purchase quantity ({item['Unit']})", min_value=0.0, step=0.1, format="%.2f")
                submitted = st.form_submit_button("Save Purchase", type="primary", use_container_width=True)
            if submitted:
                ledger = load_records(ledger_sheet)
                # Retain existing close when saving a purchase independently.
                index = existing_row(ledger, day, item["Shortcode"])
                close = as_number(ledger.loc[index, "Closing Stock"]) if index is not None else 0.0
                opening, variance = save_entry(ledger_sheet, ledger, day, item, qty, close)
                st.success(f"Saved! Opening stock: {opening:g} {item['Unit']}.")
                st.cache_data.clear()

    with closing_tab:
        item = item_picker(items, "closing") if items else None
        if item:
            with st.form("closing_form", clear_on_submit=True):
                qty = st.number_input(f"Physical closing stock ({item['Unit']})", min_value=0.0, step=0.1, format="%.2f")
                submitted = st.form_submit_button("Save Closing Stock", type="primary", use_container_width=True)
            if submitted:
                ledger = load_records(ledger_sheet)
                index = existing_row(ledger, day, item["Shortcode"])
                purchase = as_number(ledger.loc[index, "Purchases"]) if index is not None else 0.0
                opening, variance = save_entry(ledger_sheet, ledger, day, item, purchase, qty)
                st.success(f"Saved! Expected: {opening + purchase:g}; variance: {variance:g} {item['Unit']}.")

    with new_item_tab:
        with st.form("new_item", clear_on_submit=True):
            name = st.text_input("Item name", placeholder="Tomato")
            code = st.text_input("Shortcode", placeholder="TOM").upper().strip()
            category = st.text_input("Category", placeholder="Vegetables")
            unit = st.selectbox("Standard unit", ["kg", "L", "pcs"])
            added = st.form_submit_button("Add Item", type="primary", use_container_width=True)
        if added:
            if not name or not code:
                st.error("Please enter both item name and shortcode.")
            elif any(str(i["Shortcode"]).upper() == code for i in items):
                st.error("That shortcode is already in use. Choose another one.")
            else:
                master_sheet.append_row([code, name.strip(), category.strip(), unit])
                st.success(f"{name.strip()} added to the Master Stock List.")


def owner_view(ledger_sheet):
    st.subheader("Owner: History & Export")
    configured_password = secret("owner_password")
    if not configured_password:
        st.warning("Owner view is disabled until `owner_password` is set in Streamlit secrets.")
        return
    if not st.session_state.get("owner_authenticated"):
        with st.form("owner_login"):
            password = st.text_input("Owner password", type="password")
            login = st.form_submit_button("Open Owner View", type="primary")
        if login:
            if password == configured_password:
                st.session_state.owner_authenticated = True
                st.rerun()
            st.error("Incorrect password.")
        return
    ledger = load_records(ledger_sheet)
    if ledger.empty:
        st.info("No ledger entries have been saved yet.")
        return
    ledger["Date"] = ledger["Date"].astype(str)
    dates = sorted(ledger["Date"].unique(), reverse=True)
    selected_dates = st.multiselect("Show operational dates", dates, default=dates[:7])
    shown = ledger[ledger["Date"].isin(selected_dates)] if selected_dates else ledger.iloc[0:0]
    st.dataframe(shown, use_container_width=True, hide_index=True)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        ledger.to_excel(writer, index=False, sheet_name="Daily Ledger")
    st.download_button("Download complete master log (.xlsx)", output.getvalue(), "restaurant_inventory_log.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", use_container_width=True)
    if st.button("Lock Owner View"):
        st.session_state.owner_authenticated = False
        st.rerun()


def main():
    st.sidebar.title("Restaurant Inventory")
    mode = st.sidebar.radio("Choose view", ["Manager", "Owner"])
    try:
        sheets = ensure_worksheets(spreadsheet())
        if mode == "Manager":
            manager_view(sheets["Master_Items"], sheets["Daily_Ledger"])
        else:
            owner_view(sheets["Daily_Ledger"])
    except Exception as exc:
        st.error("Could not connect to Google Sheets. Check your Streamlit secrets and sheet sharing permissions.")
        st.exception(exc)


if __name__ == "__main__":
    main()
