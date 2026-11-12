# ============================================================
# NATURAL GAS BURN TRACKER 
# Main Streamlit screen
# meter readings, buy volumes, transport costs, and financials.
# ============================================================

# --- Imports ---
# pyrefly: ignore [missing-import]
import streamlit as st
import pandas as pd
import pathlib
import base64 
import os
import tempfile

from common import render_header, render_status_pill, can_edit, get_access_level, seed_shared_filter, save_shared_filter

TABLE_NAME = "CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_TRACKER_VOLUME_CALCS"

@st.fragment(run_every="45s")

def _keep_warehouse_warm():

    conn = st.connection("snowflake")

    conn.query("SELECT 1", ttl=0)

# --- Load Theme Config from config.toml ---
# Reads colors, typography, and layout settings from the project's TOML config file.
# Falls back gracefully if the file or library is unavailable.
CONFIG_PATH = pathlib.Path(__file__).parent / ".streamlit" / "config.toml"
config_data = {}
try:
    import tomllib
    with open(CONFIG_PATH, "rb") as f:
        config_data = tomllib.load(f)
except ImportError:
    try:
        import toml
        config_data = toml.load(CONFIG_PATH)
    except Exception:
        pass

colors = config_data.get("colors", {})
typography = config_data.get("typography", {})
layout = config_data.get("layout", {})

# --- Extract Theme Tokens from TOML ---
# These variables are used throughout the CSS and Python rendering logic.
FONT_FAMILY = typography.get("fontFamily", "Arial")
FONT_SIZE_MEDIUM = typography.get("fontSizeMedium", "1.05rem")
FONT_SIZE_SMALL = typography.get("fontSizeSmall", "0.85rem")
LIGHT_GRAY = colors.get("Light_Gray", "#bebebe")
PRIMARY_GREEN = colors.get("Green", "#00843d")
DARK_GREEN = colors.get("Dark_Green", "#004438")
DARK_BLUE = colors.get("Dark_Blue", "#003865")
GOLD = colors.get("Gold", "#cd8a2a")
CHARCOAL = colors.get("Charcoal", "#565a5c")
YELLOW = colors.get("Yellow", "#ffc000")
GRAY = colors.get("Gray", "#808080")
ALIGNMENT = layout.get("alignment", "center")


render_header()

def get_effective_end_ts_expr(period_str):
    """
    Returns SQL expression for RECORD_END_TS:
    - If period is older than current month: month end of the period (LAST_DAY(TO_DATE('{date_str}')))
    - If period is current month (or newer): CURRENT_TIMESTAMP()
    """
    try:
        if period_str:
            p_dt = pd.to_datetime(period_str, errors='coerce')
            if p_dt is not None and not pd.isna(p_dt):
                now = pd.Timestamp.now()
                p_month_start = p_dt.replace(day=1).date()
                curr_month_start = now.replace(day=1).date()
                if p_month_start < curr_month_start:
                    return f"LAST_DAY(TO_DATE('{p_dt.strftime('%Y-%m-%d')}'))"
    except Exception:
        pass
    return "CURRENT_TIMESTAMP()"

# Data is loaded dynamically based on filters below.

# --- Filter Mapping Loader ---
# Fetches distinct Period / Refinery / Supplier / Status combinations from Snowflake.
# Used to populate the sidebar filters and to determine the period status (Open/Prelim/Closed).
# Helper to execute multiple SQL statements in Snowflake in a single call safely
def execute_batch_sql(session, sql_statements):
    if not sql_statements:
        return
    clean_statements = [s.strip().rstrip(';') for s in sql_statements if s.strip()]
    if not clean_statements:
        return
    if len(clean_statements) == 1:
        session.sql(clean_statements[0]).collect()
    else:
        body = ";\n".join(clean_statements)
        block = f"EXECUTE IMMEDIATE $$\nBEGIN\n{body};\nEND;\n$$;"
        session.sql(block).collect()

@st.cache_data(ttl=600)
def get_refinery_id_by_name(refinery_name):
    if not refinery_name:
        return None
    try:
        conn = st.connection("snowflake")
        df = conn.query("SELECT CONFIG_ID, VALUE FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG WHERE TYPE = 'REFINERY'", ttl=600)
        ref_key = str(refinery_name).strip().upper()
        for _, r in df.iterrows():
            val = str(r['VALUE']).strip().upper()
            if val == ref_key or val.replace(" ", "") == ref_key.replace(" ", ""):
                return int(r['CONFIG_ID'])
    except Exception:
        pass
    return None

def get_sp_burn_tracker_volume_call(period_str, refinery_name):
    try:
        if not period_str:
            return None
        dt = pd.to_datetime(period_str, errors='coerce')
        if dt is None or pd.isna(dt):
            return None
        month = dt.month
        year = dt.year

        ref_id = get_refinery_id_by_name(refinery_name)
        ref_id_str = str(ref_id) if ref_id is not None else "NULL"

        return f"CALL CONSUMPTION.HFS_COMMERCIAL_ONLY.SP_BURN_TRACKER_VOLUME({ref_id_str}, {month}, {year})"
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def get_swing_purchase_buy_volumes(period_str):
    if not period_str:
        return {}
    try:
        dt = pd.to_datetime(period_str, errors='coerce')
        if dt is None or pd.isna(dt):
            return {}
        month = dt.month
        year = dt.year
        conn = st.connection("snowflake")
        query = f"""
            SELECT CALENDAR_DATE, BUY_VOLUME
            FROM CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME
            WHERE REF_SUP_MAPPING_ID = 7
              AND RECORD_END_TS IS NULL
              AND EXTRACT(MONTH FROM CALENDAR_DATE) = {month}
              AND EXTRACT(YEAR FROM CALENDAR_DATE) = {year}
        """
        df = conn.query(query, ttl=300)
        if df.empty:
            return {}
        res = {}
        for _, r in df.iterrows():
            d_str = pd.to_datetime(r['CALENDAR_DATE']).strftime('%Y-%m-%d')
            res[d_str] = float(r['BUY_VOLUME']) if pd.notnull(r['BUY_VOLUME']) else 0.0
        return res
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner="Loading filter mapping...")
def get_filter_mapping():
    conn = st.connection("snowflake")
    try:
        df_mapping = conn.query("SELECT DISTINCT PERIOD, REFINERY, SUPPLIER, STATUS FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.ng_dashboard_filter_vw", ttl=300)
        return df_mapping
    except Exception:
        return pd.DataFrame(columns=["PERIOD", "REFINERY", "SUPPLIER", "STATUS"])

df_mapping = get_filter_mapping()

# --- Build Sorted Period List ---
# Converts raw period strings to datetime for correct descending sort order.
if not df_mapping.empty and 'PERIOD' in df_mapping.columns:
    periods_unique = df_mapping['PERIOD'].dropna().unique()
    temp_df = pd.DataFrame({'PERIOD': periods_unique})
    temp_df['temp'] = pd.to_datetime(temp_df['PERIOD'], errors='coerce')
    periods_list = temp_df.dropna(subset=['temp']).sort_values(by='temp', ascending=False)['PERIOD'].astype(str).tolist()
    unparseable = [p for p in periods_unique if str(p) not in periods_list]
    periods_list.extend(unparseable)
else:
    periods_list = []

# --- Sidebar Filters ---
# Period, Refinery, Supplier (multiselect), Status badge, and Dashboard link button.
# Supplier column width is dynamically adjusted based on the number of available suppliers.

# --- Dynamic Column Width for Suppliers ---
seed_shared_filter("period", "tracker_period_final", periods_list)
temp_period = st.session_state.get("tracker_period_final", periods_list[0] if periods_list else None)
temp_ref_list = sorted(df_mapping[df_mapping['PERIOD'] == temp_period]['REFINERY'].dropna().astype(str).unique().tolist()) if not df_mapping.empty and temp_period else []
seed_shared_filter("refinery", "refinery_filter", temp_ref_list)
temp_ref = st.session_state.get("refinery_filter", temp_ref_list[0] if temp_ref_list else None)
if temp_ref not in temp_ref_list and temp_ref_list:
    temp_ref = temp_ref_list[0]
temp_sup_list = sorted(df_mapping[(df_mapping['PERIOD'] == temp_period) & (df_mapping['REFINERY'] == temp_ref)]['SUPPLIER'].dropna().astype(str).unique().tolist()) if not df_mapping.empty and temp_period and temp_ref else []

num_sups = len(temp_sup_list)
if num_sups >= 4:
    sup_width = 2.7
elif num_sups == 3:
    sup_width = 2.2
elif num_sups == 2:
    sup_width = 1.7
else:
    sup_width = 1.2

spacer_width = max(0.001, 2.7 - sup_width)

filter_cols = st.columns([0.75, 0.75, sup_width, spacer_width, 0.7, 0.7])
with filter_cols[0]:
    selected_period = st.selectbox("Period", periods_list, key="tracker_period_final")
    save_shared_filter("period", selected_period)

# Filter Refineries based on Period
if not df_mapping.empty and selected_period:
    refineries_list = sorted(df_mapping[df_mapping['PERIOD'] == selected_period]['REFINERY'].dropna().astype(str).unique().tolist())
else:
    refineries_list = []

with filter_cols[1]:
    selected_refinery = st.selectbox("Refinery", refineries_list, key="refinery_filter")
    save_shared_filter("refinery", selected_refinery)

# Filter Suppliers based on Period and Refinery
if not df_mapping.empty and selected_period and selected_refinery:
    mask = (df_mapping['PERIOD'] == selected_period) & (df_mapping['REFINERY'] == selected_refinery)
    suppliers_list = sorted(df_mapping[mask]['SUPPLIER'].dropna().astype(str).unique().tolist())
else:
    suppliers_list = []

with filter_cols[2]:
    # Using multiselect as requested instead of custom popover
    selected_suppliers = st.multiselect("Supplier", suppliers_list, default=suppliers_list)

# --- Data Loaders (Snowflake Queries) ---
# Each function queries a specific Snowflake view for the selected period.
# All are cached with st.cache_data to avoid redundant network calls.

@st.cache_data(show_spinner="Fetching burn tracker data from Snowflake...")
def get_tracker_data(period_str):
    conn = st.connection("snowflake")
    try:
        dt = pd.to_datetime(period_str)
        month = dt.month
        year = dt.year
        query = f"""
            SELECT * FROM {TABLE_NAME}
            WHERE EXTRACT(MONTH FROM Calendar_date) = {month}
            AND EXTRACT(YEAR FROM Calendar_date) = {year}
        """
        return conn.query(query)
    except Exception:
        query = f"SELECT * FROM {TABLE_NAME} WHERE TO_CHAR(Calendar_date, 'Mon YYYY') = '{period_str}'"
        return conn.query(query)

# Fetches daily meter readings (burn readings) per meter per day.
@st.cache_data(show_spinner="Fetching meter readings data...")
def get_meter_readings_data(period_str):
    conn = st.connection("snowflake")
    try:
        dt = pd.to_datetime(period_str)
        month = dt.month
        year = dt.year
        query = f"""
            SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_READING_VW
            WHERE EXTRACT(MONTH FROM Calendar_date) = {month}
            AND EXTRACT(YEAR FROM Calendar_date) = {year}
        """
        return conn.query(query)
    except Exception:
        query = f"SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_READING_VW WHERE TO_CHAR(Calendar_date, 'Mon YYYY') = '{period_str}'"
        return conn.query(query)

def is_parco_refinery(selected_refinery=None, df_readings=None):
    if selected_refinery:
        ref_upper = str(selected_refinery).strip().upper()
        if ref_upper in ["PARCO"] or get_refinery_id_by_name(selected_refinery) == 9:
            return True
    if df_readings is not None and not df_readings.empty:
        for col in ["REFINERY_NAME", "REFINERY"]:
            if col in df_readings.columns:
                vals = [str(v).strip().upper() for v in df_readings[col].dropna().unique()]
                if "PARCO" in vals:
                    return True
        if "REFINERY_ID" in df_readings.columns:
            try:
                ids = [int(v) for v in df_readings["REFINERY_ID"].dropna().unique()]
                if 9 in ids:
                    return True
            except Exception:
                pass
    return False

def is_tulsa_refinery(selected_refinery=None, df_readings=None):
    if selected_refinery:
        ref_upper = str(selected_refinery).strip().upper()
        if ref_upper in ["TULSA EAST", "TULSAEAST", "TULSA WEST", "TULSAWEST"]:
            return True
    if df_readings is not None and not df_readings.empty:
        for col in ["REFINERY_NAME", "REFINERY"]:
            if col in df_readings.columns:
                vals = [str(v).strip().upper() for v in df_readings[col].dropna().unique()]
                if any(t in vals for t in ["TULSA EAST", "TULSAEAST", "TULSA WEST", "TULSAWEST"]):
                    return True
    return False

def calculate_daily_burn_sum(day_data, is_parco=False):
    if day_data.empty or 'DAILY_BURN_READING' not in day_data.columns:
        return 0.0
    if is_parco:
        name_col = 'METER_NAME' if 'METER_NAME' in day_data.columns else None
        num_col = next((c for c in day_data.columns if 'NUMBER' in str(c).upper() or 'NO' in str(c).upper() and 'METER' in str(c).upper()), None)
        
        mask_4050 = pd.Series(False, index=day_data.index)
        mask_actual = pd.Series(False, index=day_data.index)
        
        if name_col:
            names = day_data[name_col].astype(str).str.strip().str.upper()
            mask_4050 |= names.str.contains('4050')
            mask_actual |= names.str.contains('ACTUAL|TOTAL CIG|TOTAL_CIG')
            
        if num_col:
            nums = day_data[num_col].astype(str).str.strip().str.upper()
            mask_4050 |= nums.str.contains('4050')
            mask_actual |= nums.str.contains('ACTUAL|TOTAL CIG|TOTAL_CIG')
            
        parco_mask = mask_4050 | mask_actual
        parco_rows = day_data[parco_mask]
        if not parco_rows.empty:
            return float(parco_rows['DAILY_BURN_READING'].sum())
        else:
            return float(day_data['DAILY_BURN_READING'].sum())
    else:
        return float(day_data['DAILY_BURN_READING'].sum())

# Fetches fuel usage (pipeline volume/cost) per day.
@st.cache_data(show_spinner="Fetching fuel usage data...")
def get_fuel_usage_data(period_str):
    conn = st.connection("snowflake")
    try:
        dt = pd.to_datetime(period_str)
        month = dt.month
        year = dt.year
        query = f"""
            SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_TRACKER_FUEL_USAGE_VW
            WHERE EXTRACT(MONTH FROM Calendar_date) = {month}
            AND EXTRACT(YEAR FROM Calendar_date) = {year}
        """
        return conn.query(query)
    except Exception:
        query = f"SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_TRACKER_FUEL_USAGE_VW WHERE TO_CHAR(Calendar_date, 'Mon YYYY') = '{period_str}'"
        return conn.query(query)

def get_display_burn_totals(df_readings_filtered, selected_refinery=None):
    """
    Calculates Burn Total (MMBtu) for each day:
    - Raw meter reading sum if > 0 (for PARCO: Meter 4050 + Total Cig ACTUAL).
    - Rolling average rules when Day 1 > 0 and subsequent days are 0.
      - Standard Refineries: Day 2-3 repeat Day 1, Day 4-6 expanding averages, Day 7+ 5-day rolling average.
      - Tulsa East & Tulsa West: Day 2-4 repeat Day 1, Day 5-7 expanding averages, Day 8+ 5-day rolling average.
    """
    if df_readings_filtered.empty or 'DATE_PARSED' not in df_readings_filtered.columns:
        return []
    is_parco = is_parco_refinery(selected_refinery, df_readings_filtered)
    is_tulsa = is_tulsa_refinery(selected_refinery, df_readings_filtered)
    unique_dates = df_readings_filtered[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().dropna().sort_values('DATE_PARSED')
    raw_burn_list = []
    for _, date_row in unique_dates.iterrows():
        dt = date_row['DATE_PARSED']
        day_data = df_readings_filtered[df_readings_filtered['DATE_PARSED'] == dt]
        b_sum = calculate_daily_burn_sum(day_data, is_parco=is_parco)
        raw_burn_list.append(b_sum)

    display_burn_totals = []
    n_days = len(raw_burn_list)
    day1_burn = raw_burn_list[0] if n_days > 0 else 0.0

    if day1_burn > 0:
        for i in range(n_days):
            raw_v = raw_burn_list[i]
            if i == 0:
                calc_v = raw_v
            else:
                if raw_v != 0:
                    calc_v = raw_v
                else:
                    if is_tulsa:
                        if i in (1, 2, 3):
                            calc_v = display_burn_totals[0]
                        elif i == 4:
                            slice_vals = display_burn_totals[0:2]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                        elif i == 5:
                            slice_vals = display_burn_totals[0:3]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                        elif i == 6:
                            slice_vals = display_burn_totals[0:4]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                        else:
                            slice_vals = display_burn_totals[i-7 : i-2]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                    else:
                        if i in (1, 2):
                            calc_v = display_burn_totals[0]
                        elif i == 3:
                            slice_vals = display_burn_totals[0:2]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                        elif i == 4:
                            slice_vals = display_burn_totals[0:3]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                        elif i == 5:
                            slice_vals = display_burn_totals[0:4]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
                        else:
                            slice_vals = display_burn_totals[i-6 : i-1]
                            calc_v = sum(slice_vals) / len(slice_vals) if slice_vals else 0.0
            display_burn_totals.append(calc_v)
    else:
        display_burn_totals = raw_burn_list

    return display_burn_totals

# Fetches FRM pricing region splits (FoM / Gas Daily % and rider data).
@st.cache_data(ttl=300, show_spinner="Loading FRM pricing metrics...")
def get_frm_pricing(period, refinery):
    conn = st.connection("snowflake")
    try:
        query = f"SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_FRM_PRICING_VW WHERE PERIOD = '{period}' AND REFINERY_NAME = '{refinery}'"
        return conn.query(query)
    except Exception:
        return pd.DataFrame()

# Fetches FRM price contract summary (gas cost, volume, WA price by contract type).
@st.cache_data(ttl=300, show_spinner="Loading FRM contract metrics...")
def get_frm_price_contract(period, refinery):
    conn = st.connection("snowflake")
    try:
        query = f"SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_FRM_PRICE_CONTRACT_VW WHERE PERIOD = '{period}' AND REFINERY_NAME = '{refinery}'"
        return conn.query(query)
    except Exception:
        return pd.DataFrame()

# Fetches FRM miscellaneous costs (transport, fees, imbalance, tolerance).
@st.cache_data(ttl=300, show_spinner="Loading FRM misc metrics...")
def get_frm_misc(period, refinery):
    conn = st.connection("snowflake")
    try:
        query = f"SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_FRM_MISC_VW WHERE PERIOD = '{period}' AND REFINERY_NAME = '{refinery}'"
        return conn.query(query)
    except Exception:
        return pd.DataFrame()

# Fetches carryover imbalance from monthly status table
@st.cache_data(ttl=600, show_spinner="Loading carryover imbalance...")
def get_carryover_imbalance(period_str, refinery_name):
    conn = st.connection("snowflake")
    try:
        dt = pd.to_datetime(period_str)
        month = dt.month
        year = dt.year
        
        # Get refinery_id
        master_df = conn.query("SELECT CONFIG_ID, VALUE FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG WHERE TYPE = 'REFINERY'")
        ref_id = None
        for _, r in master_df.iterrows():
            if str(r['VALUE']).strip().upper() == str(refinery_name).strip().upper():
                ref_id = int(r['CONFIG_ID'])
                break
        
        if ref_id is None:
            return 0.0
            
        query = f"""
            SELECT PREV_MONTH_IMBALANCE 
            FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MONTHLY_STATUS
            WHERE REFINERY_ID = {ref_id}
              AND PERIOD_MONTH = {month}
              AND PERIOD_YEAR = {year}
              AND RECORD_END_TS IS NULL
        """
        res = conn.query(query)
        if not res.empty and 'PREV_MONTH_IMBALANCE' in res.columns:
            val = res.iloc[0]['PREV_MONTH_IMBALANCE']
            return float(val) if pd.notnull(val) else 0.0
    except Exception:
        pass
    return 0.0

# Fetches firm transport contract data (FIRM / USAGE / IT types) from the transport contract view.
# Filtered by refinery ID and the month/year of the selected period.
@st.cache_data(ttl=300, show_spinner=True)
def get_transport_contract_data(period_str, refinery_id):
    conn = st.connection("snowflake")
    try:
        dt = pd.to_datetime(period_str)
        month = dt.month
        year = dt.year
        query = f"""
            SELECT PRICE_DATE, REFINERY_ID, TR_CONTRACT_NUMBER, TR_CONTRACT_TYPE,
                   BASE_RATE, DEMAND_VOLUME AS TR_BASE_VOLUME, TR_COST
            FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_TRANSPORT_CONTRACT_VW
            WHERE REFINERY_ID = {refinery_id}
            AND EXTRACT(MONTH FROM PRICE_DATE) = {month}
            AND EXTRACT(YEAR FROM PRICE_DATE) = {year}
        """
        return conn.query(query, ttl=300)
    except Exception:
        return pd.DataFrame()

# --- Additional Transactions Mapping Loader ---
@st.cache_data(show_spinner=True)
def get_additional_trans_mapping(selected_period):
    if not selected_period:
        return pd.DataFrame()
    conn = st.connection("snowflake")
    time_filter_map = f"""
        AND m.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY')
        AND COALESCE(DATE(m.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')
    """
    ref_tf = f"AND ref.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(ref.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    sup_tf = f"AND sup.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(sup.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    reg_tf = f"AND reg.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(reg.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    code_tf = f"AND code.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(code.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"

    query = f"""
    SELECT DISTINCT
        m.REF_SUP_MAPPING_ID,
        ref.VALUE  AS REFINERY_NAME,
        sup.VALUE    AS SUPPLIER_NAME,
        con.VALUE    AS CONTRACT_TYPE_NAME,
        reg.VALUE    AS PRICE_REGION_NAME,
        code.VALUE   AS PRICE_CODE_NAME
    FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUPPLIER_MAPPING m
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG ref 
        ON m.REFINERY_ID = ref.CONFIG_ID AND ref.TYPE = 'REFINERY' {ref_tf}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG sup 
        ON m.SUPPLIER_ID = sup.CONFIG_ID AND sup.TYPE = 'SUPPLIER' {sup_tf}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG con 
        ON m.CONTRACT_TYPE_ID = con.CONFIG_ID AND con.TYPE = 'CONTRACT_TYPE'
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG reg 
        ON m.PRICING_REGION_ID = reg.CONFIG_ID AND reg.TYPE = 'PRICING REGION' {reg_tf}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG code 
        ON m.PRICE_CODE_ID = code.CONFIG_ID AND code.TYPE = 'PRICE_CODES' {code_tf}
    WHERE 1=1
      {time_filter_map}
    """
    try:
        df = conn.query(query)
        if not df.empty:
            df = df.drop_duplicates()
        return df
    except Exception:
        return pd.DataFrame()

# ============================================================
# --- Stage File Download Config & Helper ---
# Path pattern: @<STAGE>/Archive/<YYYYMM>/<FILE_PREFIX>_<YYYYMMDD>.pdf
# YYYYMM / YYYYMMDD are both derived from the selected Period
# (YYYYMMDD = last calendar day of that period's month).
# ============================================================
OCR_STAGE_PATH = "@RAW.HFS_COMMERCIAL_ONLY.OCR_STAGE"  # TODO: confirm fully-qualified name, e.g. "@CONFORMED.HFS_COMMERCIAL_ONLY.OCR_STAGE"

REFINERY_STAGE_FILE_MAP = {
    "EL DORADO":   "DAILY_RHONDA_SHUTTS - DailyVolumeStatement",
    "ELDORADO":    "DAILY_RHONDA_SHUTTS - DailyVolumeStatement",
    "PUGET SOUND": "HF Plant Sheet",
    "PUGETSOUND":  "HF Plant Sheet",
    "ARTESIA":     "report_data",
    "ARTESIA RDU": "report_data",
    "ARTESIARDU":  "report_data",
    "TULSA EAST":  "SRE_GVSDaily-1757370_11970080HollyRefining - DailyVolumeStatement",
    "TULSAEAST":   "SRE_GVSDaily-1757370_11970080HollyRefining - DailyVolumeStatement",
    "TULSA WEST":  "SRE_GVSDaily-1757370_11970080HollyRefining - DailyVolumeStatement",
    "TULSAWEST":   "SRE_GVSDaily-1757370_11970080HollyRefining - DailyVolumeStatement",
    "WOOD CROSS":  "WEB LOCATION NOMS VS USE",
    "WOODCROSS":   "WEB LOCATION NOMS VS USE",
    "CHEYENNE":    "HOLLYFRONTIERVOLUMESEOD - DailyVolumeStatement",
    "PARCO":       "Parco Casper Input File",
    "CASPER":      "Parco Casper Input File",
}
 
def get_stage_dir_and_prefix(period_str, refinery_name):
    """Returns (stage_dir_path, file_prefix, yyyymmdd) or None if unmapped."""
    if not refinery_name or not period_str:
        return None
    ref_key = str(refinery_name).strip().upper()
    file_prefix = REFINERY_STAGE_FILE_MAP.get(ref_key) or REFINERY_STAGE_FILE_MAP.get(ref_key.replace(" ", ""))
    if not file_prefix:
        return None
    p_dt = pd.to_datetime(period_str, errors="coerce")
    if p_dt is None or pd.isna(p_dt):
        return None
    yyyymm = p_dt.strftime("%Y%m")
    yyyymmdd = (p_dt + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")
    stage_dir = f"{OCR_STAGE_PATH}/Archive/{yyyymm}"
    return stage_dir, file_prefix, yyyymmdd



@st.cache_data(ttl=300, show_spinner=True)
def list_stage_directory(_session, stage_dir_path):
    """Queries a Snowflake stage directory via LIST — returns file name, size,
    and last-modified time without downloading anything.
    """
    try:
        # LIST without quotes around stage path
        rows = _session.sql(f"LIST {stage_dir_path}").collect()
    except Exception as e:
        return [], str(e)
    files = []
    for r in rows:
        d = r.as_dict()
        full_name = d.get("name") if d.get("name") is not None else d.get("NAME", "")
        last_mod = d.get("last_modified") if d.get("last_modified") is not None else d.get("LAST_MODIFIED")
        size_val = d.get("size") if d.get("size") is not None else d.get("SIZE")
        files.append({
            "full_path": str(full_name),
            "base_name": str(full_name).split("/")[-1],
            "size": size_val,
            "last_modified": last_mod,
        })
    return files, None
    

import glob

@st.cache_data(ttl=300, show_spinner=True)
def download_stage_file_bytes(_session, stage_dir_path, file_prefix, yyyymmdd):
    """Finds the latest matching file for the selected period/refinery."""
    files, err = list_stage_directory(_session, stage_dir_path)
    if err or not files:
        return None, None, f"File not found for the selected period/refinery."

    prefix_upper = file_prefix.strip().upper()

    def is_match(base_name):
        base_upper = base_name.strip().upper()
        if prefix_upper in base_upper or base_upper.startswith(prefix_upper):
            return True
        keywords = [k for k in prefix_upper.split() if len(k) > 3 and k not in ("FILE", "INPUT", "STATEMENT", "DAILY")]
        if keywords and all(kw in base_upper for kw in keywords):
            return True
        return False

    matching_files = [f for f in files if is_match(f["base_name"])]
    if not matching_files:
        return None, None, "File not found for the selected period/refinery."

    def _get_file_ts(f):
        val = f.get("last_modified")
        if not val:
            return pd.Timestamp.min
        try:
            ts = pd.to_datetime(val)
            return ts if pd.notnull(ts) else pd.Timestamp.min
        except Exception:
            return pd.Timestamp.min

    # Pick the latest (most recently modified) file by Snowflake's LAST_MODIFIED timestamp
    matching_files.sort(key=_get_file_ts, reverse=True)
    match = matching_files[0]

    stage_file_candidates = [
        f"{stage_dir_path}/'{match['base_name']}'",
        f"{stage_dir_path}/{match['base_name']}",
        f"@{match['full_path']}" if match.get('full_path') and not str(match['full_path']).startswith('@') else match.get('full_path'),
        f"'{stage_dir_path}/{match['base_name']}'"
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        get_results = None
        last_err = None
        for stage_path_attempt in stage_file_candidates:
            if not stage_path_attempt:
                continue
            try:
                res = _session.file.get(stage_path_attempt, tmp_dir)
                if res and len(res) > 0 and res[0].status == "DOWNLOADED":
                    get_results = res
                    break
            except Exception as e:
                last_err = e

        if not get_results:
            return None, None, f"Download failed: {last_err if last_err else 'File not found on stage'}"

        target_name = os.path.basename(get_results[0].file)
        matches = glob.glob(os.path.join(tmp_dir, "**", target_name), recursive=True)
        if not matches:
            return None, None, "File not found for the selected period/refinery."

        with open(matches[0], "rb") as f:
            data = f.read()
        return data, match["base_name"], None
        
# --- Additional Transactions Fetch Function ---
@st.cache_data(show_spinner=True)
def fetch_additional_transactions(refinery_name, date_min_str, date_max_str, selected_period):
    if not refinery_name or not date_min_str or not date_max_str or not selected_period:
        return pd.DataFrame()
    conn = st.connection("snowflake")
    time_filter_map = f"AND m.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(m.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    ref_tf = f"AND ref.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(ref.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    sup_tf = f"AND sup.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(sup.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    reg_tf = f"AND reg.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(reg.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"
    code_tf = f"AND code.RECORD_START_TS <= TO_DATE('{selected_period}', 'MON YYYY') AND COALESCE(DATE(code.RECORD_END_TS), '9999-12-31') > TO_DATE('{selected_period}', 'MON YYYY')"

    query = f"""
    SELECT DISTINCT
        TO_CHAR(t.CALENDAR_DATE, 'MM/DD/YYYY') AS "Date",
        t.TRANSACTION_TYPE AS "Transaction Type",
        (
            COALESCE(ref.VALUE, '') || ' | ' ||
            COALESCE(sup.VALUE, '') || ' | ' ||
            COALESCE(con.VALUE, '') || ' | ' ||
            COALESCE(reg.VALUE, '') || ' | ' ||
            COALESCE(code.VALUE, '')
        ) AS "Mapping",
        TO_CHAR(t.TRANSACTION_VOLUME, '999,999,999') AS "Volume",
        TO_CHAR(t.TRANSACTION_PRICE, '990.00') AS "Price",
        t.AMOUNT AS "Amount",
        t.REF_SUP_MAPPING_ID AS "REF_SUP_MAPPING_ID"
    FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_ADDITIONAL_TRANSACTION t
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUPPLIER_MAPPING m
        ON t.REF_SUP_MAPPING_ID = m.REF_SUP_MAPPING_ID {time_filter_map}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG ref 
        ON m.REFINERY_ID = ref.CONFIG_ID AND ref.TYPE = 'REFINERY' {ref_tf}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG sup 
        ON m.SUPPLIER_ID = sup.CONFIG_ID AND sup.TYPE = 'SUPPLIER' {sup_tf}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG con 
        ON m.CONTRACT_TYPE_ID = con.CONFIG_ID AND con.TYPE = 'CONTRACT_TYPE'
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG reg 
        ON m.PRICING_REGION_ID = reg.CONFIG_ID AND reg.TYPE = 'PRICING REGION' {reg_tf}
    JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG code 
        ON m.PRICE_CODE_ID = code.CONFIG_ID AND code.TYPE = 'PRICE_CODES' {code_tf}
    WHERE t.RECORD_END_TS IS NULL
      AND UPPER(ref.VALUE) = '{refinery_name.upper()}'
      AND t.CALENDAR_DATE >= '{date_min_str}'
      AND t.CALENDAR_DATE <= '{date_max_str}'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.CALENDAR_DATE, t.REF_SUP_MAPPING_ID, t.TRANSACTION_TYPE, t.TRANSACTION_VOLUME, t.TRANSACTION_PRICE ORDER BY t.RECORD_START_TS DESC) = 1
    """
    try:
        df = conn.query(query)
        if not df.empty:
            df["Volume"] = df["Volume"].astype(str).str.strip()
            df["Price"] = df["Price"].astype(str).str.strip()
            if "Transaction Type" in df.columns:
                df["Transaction Type"] = df["Transaction Type"].astype(str).str.strip().str.capitalize()
            dedup_cols = [c for c in ["Date", "Transaction Type", "Mapping", "Volume", "Price", "Amount"] if c in df.columns]
            df = df.drop_duplicates(subset=dedup_cols)
            if "Date" in df.columns:
                df["_dt_temp"] = pd.to_datetime(df["Date"], errors="coerce")
                df = df.sort_values(by="_dt_temp", ascending=True).drop(columns=["_dt_temp"]).reset_index(drop=True)
            else:
                df = df.reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()

# --- Grid Imbalance and Total Buy Calculator for FRM ---
def compute_grid_imbalance_and_buy_volume(selected_period, selected_refinery, df_filtered, df_readings_filtered, df_fuel_filtered):
    if not selected_period or not selected_refinery or df_readings_filtered.empty:
        return 0.0, 0.0

    carryover_val = get_carryover_imbalance(selected_period, selected_refinery)

    if not df_filtered.empty:
        dtb_col = next((c for c in df_filtered.columns if str(c).upper() == 'DAILY_TOTAL_BUY'), None)
        if dtb_col:
            daily_buy_totals = df_filtered[['DATE_PARSED', dtb_col]].drop_duplicates(subset=['DATE_PARSED'])
            daily_buy_totals = daily_buy_totals.rename(columns={dtb_col: 'DAILY_TOTAL_BUY'})
        else:
            daily_buy_totals = df_filtered.groupby(['DATE_PARSED'])['VOLUME'].sum().reset_index()
            daily_buy_totals = daily_buy_totals.rename(columns={'VOLUME': 'DAILY_TOTAL_BUY'})
    else:
        daily_buy_totals = pd.DataFrame(columns=['DATE_PARSED', 'DAILY_TOTAL_BUY'])

    unique_dates = df_readings_filtered[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().dropna().sort_values('DATE_PARSED')
    if unique_dates.empty:
        return carryover_val, 0.0

    unique_dates = pd.merge(unique_dates, daily_buy_totals, on='DATE_PARSED', how='left')
    unique_dates['DAILY_TOTAL_BUY'] = unique_dates['DAILY_TOTAL_BUY'].fillna(0)

    is_parco_ref = is_parco_refinery(selected_refinery, df_readings_filtered)
    display_burn_totals = get_display_burn_totals(df_readings_filtered, selected_refinery)

    pipe_col = 'PIPELINE_NAME'
    vol_col = 'R_VOLUME'
    if not df_fuel_filtered.empty:
        df_fuel_temp = df_fuel_filtered.copy()
        df_fuel_temp.columns = [str(c).upper() for c in df_fuel_temp.columns]
    else:
        df_fuel_temp = pd.DataFrame()

    pipelines = sorted([p for p in df_fuel_temp[pipe_col].dropna().unique() if str(p).strip() != ""]) if (not df_fuel_temp.empty and pipe_col in df_fuel_temp.columns) else []

    cumulative_mmbtu = carryover_val
    sum_total_buy = 0.0

    for r_idx, (_, date_row) in enumerate(unique_dates.iterrows()):
        dt = date_row['DATE_PARSED']
        daily_total_buy = float(date_row['DAILY_TOTAL_BUY']) if pd.notnull(date_row['DAILY_TOTAL_BUY']) else 0.0
        sum_total_buy += daily_total_buy

        day_data = df_readings_filtered[df_readings_filtered['DATE_PARSED'] == dt]
        daily_total_burn = calculate_daily_burn_sum(day_data, is_parco=is_parco_ref)
        display_burn = display_burn_totals[r_idx] if r_idx < len(display_burn_totals) else daily_total_burn

        daily_fuel_vol = 0.0
        if pipelines and not df_fuel_temp.empty and 'DATE_PARSED' in df_fuel_temp.columns:
            fuel_date_data = df_fuel_temp[df_fuel_temp['DATE_PARSED'] == dt]
            if not fuel_date_data.empty:
                for pipe in pipelines:
                    pipe_date_data = fuel_date_data[fuel_date_data[pipe_col] == pipe]
                    if not pipe_date_data.empty and vol_col in pipe_date_data.columns:
                        v_raw = pipe_date_data[vol_col].iloc[0]
                        try: v_val = float(v_raw) if pd.notna(v_raw) and v_raw is not None else 0.0
                        except Exception: v_val = 0.0
                        daily_fuel_vol += v_val

        daily_mmbtu = daily_total_buy - display_burn - daily_fuel_vol
        cumulative_mmbtu += daily_mmbtu

    return cumulative_mmbtu, sum_total_buy

# --- Financial Review Metrics Renderer ---
# Renders the FRM section directly using Streamlit layout components.
# Uses three data frames: pricing splits, price contracts, and misc costs.
# Layout: ROW 1 (header boxes) | ROW 2 (main table + WACOG) | ROW 3 (pricing + misc + transport)
def render_frm_section(df_pricing, df_contract, df_misc, total_burn=0.0, grid_imbalance=0.0, grid_total_buy=0.0):
    # Helper: safely extract a single numeric value from a filtered dataframe row.
    def safe_get(df, filter_col, filter_val, target_col, default=0.0):
        if df.empty or filter_col not in df.columns or target_col not in df.columns:
            return default
        res = df[df[filter_col].astype(str).str.upper() == str(filter_val).upper()]
        if not res.empty:
            try: return float(res.iloc[0][target_col])
            except: return default
        return default

    # Helper: safely sum a numeric column from a dataframe, returning 0 on empty/missing.
    def safe_sum(df, target_col, default=0.0):
        if df.empty or target_col not in df.columns:
            return default
        return pd.to_numeric(df[target_col], errors='coerce').sum()

    # Helper: safely compute the mean of a numeric column, returning default on empty/NaN.
    def safe_mean(df, target_col, default=0.0):
        if df.empty or target_col not in df.columns:
            return default
        val = pd.to_numeric(df[target_col], errors='coerce').mean()
        return val if not pd.isna(val) else default

    # Helper: format value in accounting style (negative in parentheses, zero as dash if requested).
    def fmt_acct(val, zero_dash=False):
        if pd.isna(val):
            val = 0
        if val == 0 and zero_dash:
            return "$ -"
        if val < 0:
            return f"$ ({abs(val):,.2f})"
        return f"$ {val:,.2f}"

    # --- Aggregate contract-level values ---
    fom_cost = safe_get(df_contract, 'CONTRACT_TYPE', 'First of Month', 'GAS_COST')
    # Modified by sreeharismitha: Count Spot Price transactions with Gas Daily
    gda_cost = safe_get(df_contract, 'CONTRACT_TYPE', 'Gas Daily', 'GAS_COST') + safe_get(df_contract, 'CONTRACT_TYPE', 'Spot Price', 'GAS_COST')
    total_cost = fom_cost + gda_cost

    fom_vol = safe_get(df_contract, 'CONTRACT_TYPE', 'First of Month', 'TOTAL_VOLUME')
    # Modified by sreeharismitha: Count Spot Price transactions with Gas Daily
    gda_vol = safe_get(df_contract, 'CONTRACT_TYPE', 'Gas Daily', 'TOTAL_VOLUME') + safe_get(df_contract, 'CONTRACT_TYPE', 'Spot Price', 'TOTAL_VOLUME')
    total_vol = fom_vol + gda_vol

    fom_vol_pct = (fom_vol / total_vol * 100) if total_vol else 0
    gda_vol_pct = (gda_vol / total_vol * 100) if total_vol else 0
    total_vol_pct = fom_vol_pct + gda_vol_pct

    fom_wa = safe_get(df_contract, 'CONTRACT_TYPE', 'First of Month', 'WA_PRICE')
    # Modified by sreeharismitha: Weighted average price for Gas Daily including Spot Price
    gda_wa = (gda_cost / gda_vol) if gda_vol else safe_get(df_contract, 'CONTRACT_TYPE', 'Gas Daily', 'WA_PRICE')
    # Modified by sreeharismitha: True blended Weighted Average Price across all purchases
    total_wa = (total_cost / total_vol) if total_vol else 0.0

    # --- Aggregate pricing-level values ---
    total_fuel_expense = safe_mean(df_pricing, 'TOTAL_FUEL_EXPENSES')

    fom_rider = safe_mean(df_pricing, 'FOM_RIDER_AVG')
    gda_rider = safe_mean(df_pricing, 'GDA_RIDER_AVG')
    tot_rider = safe_mean(df_pricing, 'TOTAL_RIDER_AVG')
    wacog_pur = safe_mean(df_pricing, 'WACOG_PUR')
    wacog_used = safe_mean(df_pricing, 'WACOG_USED')

    # --- Aggregate misc cost values ---
    ft_transport = safe_sum(df_misc, 'FT_TRANSPORT')
    it_transport = safe_sum(df_misc, 'IT_TRANSPORT')
    usage_transport = safe_sum(df_misc, 'USAGE_TRANSPORT')
    fees_taxes = safe_sum(df_misc, 'FEES_TAXES')
    total_misc_cost = safe_sum(df_misc, 'MISC_COSTS')
    if total_misc_cost == 0:
        total_misc_cost = ft_transport + it_transport + usage_transport + fees_taxes

    firm_split = safe_mean(df_misc, 'FT_COST_SPLIT')
    it_split = safe_mean(df_misc, 'IT_COST_SPLIT')
    usage_split = safe_mean(df_misc, 'USAGE_COST_SPLIT')

    imbalance = grid_imbalance
    tolerance = (grid_imbalance / grid_total_buy * 100.0) if grid_total_buy != 0 else 0.0

    # --- Build per-region pricing split HTML rows ---
    regions_fom_html = ""
    regions_gda_html = ""
    if not df_pricing.empty:
        # Dynamically find region column and numeric split columns
        region_col = next((c for c in df_pricing.columns if 'REGION' in str(c).upper()), df_pricing.columns[0])
        split_cols = [c for c in df_pricing.columns if 'SPLIT' in str(c).upper()]
        
        if region_col in df_pricing.columns and split_cols:
            agg_dict = {c: 'max' for c in split_cols}
            df_pricing_grouped = df_pricing.groupby(region_col, as_index=False).agg(agg_dict)
        else:
            df_pricing_grouped = df_pricing

        fom_col = next((c for c in df_pricing.columns if 'FOM' in str(c).upper() and 'SPLIT' in str(c).upper()), 'FOM_PRICE_SPLIT')
        gda_col = next((c for c in df_pricing.columns if ('GDA' in str(c).upper() or 'GD' in str(c).upper()) and 'SPLIT' in str(c).upper()), 'GDA_PRICE_SPLIT')

        for _, row in df_pricing_grouped.iterrows():
            region = row.get(region_col, '')
            fom_split_val = row.get(fom_col, 0)
            if pd.isna(fom_split_val): fom_split_val = 0
            gda_split_val = row.get(gda_col, 0)
            if pd.isna(gda_split_val): gda_split_val = 0

            # Ensure split percentage never exceeds 100%
            fom_split_disp = min(100.0, max(0.0, float(fom_split_val)))
            gda_split_disp = min(100.0, max(0.0, float(gda_split_val)))

            if fom_split_disp > 0:
                regions_fom_html += f"<tr><td>{region}</td><td class='right-align'>{fom_split_disp:,.0f}%</td></tr>"
            if gda_split_disp > 0:
                regions_gda_html += f"<tr><td>{region}</td><td class='right-align'>{gda_split_disp:,.0f}%</td></tr>"

    if not regions_fom_html:
        regions_fom_html = "<tr><td>-</td><td class='right-align'>0%</td></tr>"
    if not regions_gda_html:
        regions_gda_html = "<tr><td>-</td><td class='right-align'>0%</td></tr>"

    # --- Shared CSS (regular string, not f-string) ---
    css = """
    <style>
    .frm-tbl { border-collapse: collapse; font-family: Arial, sans-serif; font-size: 13px; width: 100%; }
    .frm-tbl th, .frm-tbl td { border: 1px solid #d3d3d3; padding: 5px 12px; white-space: nowrap; }
    .frm-tbl th { color: #00843d; font-weight: bold; }
    .right-align { text-align: right; }
    .total-row td { font-weight: bold; background-color: #f7dfd4; }
    .frm-hdr-box {
        background-color: #00843d;
        color: white;
        border: 1px solid #00843d;
        padding: 6px 12px;
        text-align: center;
        border-radius: 8px;
        display: flex;
        justify-content: center;
        align-items: center;
        gap: 6px;
        white-space: nowrap;
        margin-bottom: 15px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .frm-hdr-box.red-bg {
        background-color: #da291c !important;
        border-color: #da291c !important;
    }
    .frm-hdr-box .hdr-title { font-weight: bold; font-size: 13px; color: white; margin: 0; }
    .frm-hdr-box .hdr-value { font-size: 13px; font-weight: bold; color: white; margin: 0; }
    .frm-sec-hdr {
        background-color: #00843d !important;
        color: white !important;
        font-weight: bold;
        padding: 6px 12px;
        border: 1px solid #00843d !important;
        text-align: center;
    }
    .frm-btm-tbl { border-collapse: collapse; font-family: Arial, sans-serif; font-size: 13px; width: 100%; }
    .frm-btm-tbl th, .frm-btm-tbl td { border: 1px solid #d3d3d3; padding: 4px 12px; white-space: nowrap; }
    .frm-btm-tbl th { color: #00843d; font-weight: bold; }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)

    # --- ROW 1: Header Boxes (4 KPI Cards across single row) ---
    hdr_cols = st.columns(4)
    with hdr_cols[0]:
        st.markdown(f'<div class="frm-hdr-box"><span class="hdr-title">Total Fuel Gas Expense:</span> <span class="hdr-value">$ {total_fuel_expense:,.2f}</span></div>', unsafe_allow_html=True)
    with hdr_cols[1]:
        st.markdown(f'<div class="frm-hdr-box"><span class="hdr-title">Total Burn:</span> <span class="hdr-value">{total_burn:,.0f} MMBtu</span></div>', unsafe_allow_html=True)
    with hdr_cols[2]:
        imbalance_str = f"{imbalance:,.0f} MMBtu" if (abs(imbalance - round(imbalance)) < 1e-5) else f"{imbalance:,.2f} MMBtu"
        st.markdown(f'<div class="frm-hdr-box"><span class="hdr-title">Imbalance:</span> <span class="hdr-value">{imbalance_str}</span></div>', unsafe_allow_html=True)
    with hdr_cols[3]:
        red_cls = " red-bg" if abs(tolerance) > 10 else ""
        st.markdown(f'<div class="frm-hdr-box{red_cls}"><span class="hdr-title">Tolerance:</span> <span class="hdr-value">{tolerance:.3f}%</span></div>', unsafe_allow_html=True)

    # --- ROW 2: Main Summary Table + WACOG ---
    main_cols = st.columns([5, 1.5])
    with main_cols[0]:
        main_html = f"""<table class="frm-tbl">
        <tr>
            <th style="border-top: none !important; border-left: none !important; border-right: none !important; background: white;"></th>
            <th>Gas Cost ($)</th>
            <th>Volume (MMBtu)</th>
            <th>Volume Split (%)</th>
            <th>WA Price ($/MMBtu)</th>
            <th>Rider ($)</th>
        </tr>
        <tr>
            <td>Baseload (FoM)</td>
            <td class="right-align">{fmt_acct(fom_cost)}</td>
            <td class="right-align">{fom_vol:,.2f}</td>
            <td class="right-align">{fom_vol_pct:,.2f}</td>
            <td class="right-align">{fmt_acct(fom_wa)}</td>
            <td class="right-align">$ {fom_rider:,.2f}</td>
        </tr>
        <tr>
            <td>Gas Daily (GD)</td>
            <td class="right-align">{fmt_acct(gda_cost)}</td>
            <td class="right-align">{gda_vol:,.2f}</td>
            <td class="right-align">{gda_vol_pct:,.2f}</td>
            <td class="right-align">{fmt_acct(gda_wa)}</td>
            <td class="right-align">$ {gda_rider:,.2f}</td>
        </tr>
        <tr class="total-row">
            <td>Totals</td>
            <td class="right-align">{fmt_acct(total_cost)}</td>
            <td class="right-align">{total_vol:,.2f}</td>
            <td class="right-align">{total_vol_pct:,.2f}</td>
            <td class="right-align">{fmt_acct(total_wa)}</td>
            <td class="right-align">$ {tot_rider:,.2f}</td>
        </tr>
        </table>"""
        st.markdown(main_html, unsafe_allow_html=True)

    with main_cols[1]:
        wacog_html = f"""<table class="frm-tbl">
        <tr><th colspan="2">WACOG ($/MMBtu)</th></tr>
        <tr><td>WACOG Purchase</td><td class="right-align">{fmt_acct(wacog_pur)}</td></tr>
        <tr><td>WACOG Used</td><td class="right-align">{fmt_acct(wacog_used)}</td></tr>
        </table>"""
        st.markdown(wacog_html, unsafe_allow_html=True)

    # --- ROW 3: Bottom Tables ---
    btm_cols = st.columns([5, 5, 5, 4.5])
    with btm_cols[0]:
        fom_html = f"""<table class="frm-btm-tbl">
        <tr><td colspan="2" class="frm-sec-hdr">Pricing - FoM</td></tr>
        <tr><th>Pricing Region</th><th>% Split</th></tr>
        {regions_fom_html}
        </table>"""
        st.markdown(fom_html, unsafe_allow_html=True)

    with btm_cols[1]:
        gd_html = f"""<table class="frm-btm-tbl">
        <tr><td colspan="2" class="frm-sec-hdr">Pricing - Gas Daily</td></tr>
        <tr><th>Pricing Region</th><th>% Split</th></tr>
        {regions_gda_html}
        </table>"""
        st.markdown(gd_html, unsafe_allow_html=True)

    with btm_cols[2]:
        misc_rows = []
        misc_rows.append(f"<tr><td>FT Transport</td><td class='right-align'>{fmt_acct(ft_transport, zero_dash=True)}</td></tr>")
        misc_rows.append(f"<tr><td>IT Transport</td><td class='right-align'>{fmt_acct(it_transport, zero_dash=True)}</td></tr>")
        if 'USAGE_TRANSPORT' in df_misc.columns or usage_transport != 0:
            misc_rows.append(f"<tr><td>Usage Transport</td><td class='right-align'>{fmt_acct(usage_transport, zero_dash=True)}</td></tr>")
        misc_rows.append(f"<tr><td>Fees / Taxes</td><td class='right-align'>{fmt_acct(fees_taxes, zero_dash=True)}</td></tr>")

        misc_html = f"""<table class="frm-btm-tbl">
        <tr><td colspan="2" class="frm-sec-hdr">Miscellaneous Cost</td></tr>
        {"".join(misc_rows)}
        <tr class="total-row"><td>Total Misc Cost</td><td class="right-align">{fmt_acct(total_misc_cost)}</td></tr>
        </table>"""
        st.markdown(misc_html, unsafe_allow_html=True)

    with btm_cols[3]:
        trans_rows = []
        trans_rows.append(f"<tr><td>FIRM Cost Split %</td><td class='right-align'>{firm_split:,.0f}%</td></tr>")
        trans_rows.append(f"<tr><td>IT Cost Split %</td><td class='right-align'>{it_split:,.0f}%</td></tr>")
        if 'USAGE_COST_SPLIT' in df_misc.columns or usage_split != 0:
            trans_rows.append(f"<tr><td>Usage Cost Split %</td><td class='right-align'>{usage_split:,.0f}%</td></tr>")

        trans_html = f"""<table class="frm-btm-tbl">
        <tr><td colspan="2" class="frm-sec-hdr">Transport (MMBtu)</td></tr>
        {"".join(trans_rows)}
        </table>"""
        st.markdown(trans_html, unsafe_allow_html=True)


# ============================================================
# --- Load Raw Data Based on Selected Period ---
# Fetches all primary data frames once; downstream sections
# filter these frames by refinery / supplier as needed.
# ============================================================
if selected_period:
    with st.spinner(f"Loading Tracker data for {selected_period} - {selected_refinery}..."):
        df_raw = get_tracker_data(selected_period)
        df_readings_raw = get_meter_readings_data(selected_period)
        df_fuel_raw = get_fuel_usage_data(selected_period)
else:
    df_raw = pd.DataFrame()
    df_readings_raw = pd.DataFrame()
    df_fuel_raw = pd.DataFrame()


# --- Date Parsing ---
# Normalises the calendar date column into a consistent DATE_PARSED Timestamp column
# and adds DISPLAY_DAY (e.g. "Mon, Jul 16") and MONTH_STR for UI display.
date_col = "CALENDAR_DATE" if "CALENDAR_DATE" in [str(c).upper() for c in df_raw.columns] else "DAY"
actual_date_col = next((c for c in df_raw.columns if str(c).upper() == date_col), None)

refinery_col = "REFINERY"
daily_mmbtu_col = "DAILY_MMBTU"
cum_mmbtu_col = "CUMULATIVE_MMBTU"

if not df_raw.empty and actual_date_col and 'DATE_PARSED' not in df_raw.columns:
    try:
        if df_raw[actual_date_col].dtype == object or isinstance(df_raw[actual_date_col].iloc[0], str):
            df_raw['DATE_PARSED'] = pd.to_datetime(df_raw[actual_date_col], format="%d-%m-%Y", errors="coerce")
            if df_raw['DATE_PARSED'].isna().all():
                df_raw['DATE_PARSED'] = pd.to_datetime(df_raw[actual_date_col], errors="coerce")
        else:
            df_raw['DATE_PARSED'] = pd.to_datetime(df_raw[actual_date_col])
        if df_raw['DATE_PARSED'].isna().any():
            df_raw['DATE_PARSED'] = df_raw['DATE_PARSED'].fillna(pd.to_datetime(df_raw[actual_date_col], errors="coerce"))
    except Exception as e:
        st.error(f"Date column parsing failed: {e}")

if not df_raw.empty and 'DATE_PARSED' in df_raw.columns:
    try:
        df_raw['MONTH_STR'] = df_raw['DATE_PARSED'].dt.strftime('%B %Y') 
        df_raw['DISPLAY_DAY'] = df_raw['DATE_PARSED'].apply(lambda x: f"{x.strftime('%a, %b')} {x.day}" if pd.notnull(x) else "")
        df_raw = df_raw.sort_values(by='DATE_PARSED')
    except Exception as e:
        st.error(f"Date formatting failed: {e}")

# --- Filter Raw Data by Selected Refinery ---
# Creates working copies of the three data frames (volumes, meter readings, fuel usage)
# filtered to the currently selected refinery.
refinery_col = "REFINERY_NAME" if "REFINERY_NAME" in df_raw.columns else "REFINERY"
if not df_raw.empty and refinery_col in df_raw.columns:
    mask = (df_raw[refinery_col] == selected_refinery)
    df_filtered = df_raw[mask].copy()
else:
    df_filtered = df_raw.copy()

if not df_readings_raw.empty and "REFINERY_NAME" in df_readings_raw.columns:
    df_readings_filtered = df_readings_raw[df_readings_raw["REFINERY_NAME"].str.upper() == selected_refinery.upper()].copy()
elif not df_readings_raw.empty and "REFINERY" in df_readings_raw.columns:
    df_readings_filtered = df_readings_raw[df_readings_raw["REFINERY"].str.upper() == selected_refinery.upper()].copy()
else:
    df_readings_filtered = df_readings_raw.copy()

if not df_fuel_raw.empty and "REFINERY_NAME" in df_fuel_raw.columns:
    df_fuel_filtered = df_fuel_raw[df_fuel_raw["REFINERY_NAME"].str.upper() == selected_refinery.upper()].copy()
elif not df_fuel_raw.empty and "REFINERY" in df_fuel_raw.columns:
    df_fuel_filtered = df_fuel_raw[df_fuel_raw["REFINERY"].str.upper() == selected_refinery.upper()].copy()
else:
    df_fuel_filtered = df_fuel_raw.copy()

if not df_fuel_filtered.empty:
    if "CALENDAR_DATE" in df_fuel_filtered.columns:
        df_fuel_filtered['DATE_PARSED'] = pd.to_datetime(df_fuel_filtered['CALENDAR_DATE'], errors='coerce')
    elif "DAY" in df_fuel_filtered.columns:
        df_fuel_filtered['DATE_PARSED'] = pd.to_datetime(df_fuel_filtered['DAY'], errors='coerce')

if not df_readings_filtered.empty:
    if "CALENDAR_DATE" in df_readings_filtered.columns:
        df_readings_filtered['DATE_PARSED'] = pd.to_datetime(df_readings_filtered['CALENDAR_DATE'], errors='coerce')
    df_readings_filtered['DISPLAY_DAY'] = df_readings_filtered['DATE_PARSED'].apply(lambda x: f"{x.strftime('%a, %b')} {x.day}" if pd.notnull(x) else "")


# --- Period Status Resolution ---
# Looks up the STATUS for the selected period + refinery combination.
# Maps status to a color token: Green = Closed, Yellow = Preliminary, Gray = Open.
status_val = ""
if not df_mapping.empty and selected_period and selected_refinery:
    mask = (df_mapping['PERIOD'] == selected_period) & (df_mapping['REFINERY'] == selected_refinery)
    val = df_mapping[mask]['STATUS'].dropna()
    if not val.empty:
        status_val = str(val.iloc[0]).strip()

status_lower = status_val.lower()
if "closed" in status_lower:
    status_color = PRIMARY_GREEN
elif "prelim" in status_lower:
    status_color = YELLOW
else:
    status_color = GRAY
    
status_display = status_val

# --- User RBAC Check ---
has_edit_access = (get_access_level(selected_refinery) == "EDIT")

with filter_cols[4]:
    render_status_pill(status_val)

with filter_cols[5]:
    st.markdown(f"""
    <style>
    div[data-testid="stLinkButton"] > a {{
        background-color: {PRIMARY_GREEN} !important;
        border: 1px solid {PRIMARY_GREEN} !important;
        color: white !important;
        border-radius: 4px !important;
        min-height: 39px !important;
        font-weight: 400 !important;
        transition: all 0.2s ease-in-out;
    }}
    div[data-testid="stLinkButton"] > a p {{
        color: white !important;
    }}
    div[data-testid="stLinkButton"] > a:hover {{
        background-color: {PRIMARY_GREEN} !important;
        border-color: {PRIMARY_GREEN} !important;
        color: white !important;
        filter: brightness(0.9);
    }}
    div[data-testid="stLinkButton"] > a:active,
    div[data-testid="stLinkButton"] > a:focus {{
        background-color: {PRIMARY_GREEN} !important;
        border-color: {PRIMARY_GREEN} !important;
        color: white !important;
    }}
    </style>
    <div style="margin-top: 29px;"></div>
    """, unsafe_allow_html=True)
    st.link_button("Dashboard", url="https://app.powerbi.com/groups/19c8fe07-1f3f-4bd3-8a9d-72faaf59eda3/reports/5fc6b8a3-0529-490c-ab02-d8e4281dba3a/21704a936ee891e26e38?ctid=080430e0-a4d8-4520-8216-2e0d1aa86eca&experience=power-bi", use_container_width=True, key="dashboard_btn_t2")

st.markdown("<br>", unsafe_allow_html=True)

# --- Financial Review Metrics Expander Container ---
# Container created upfront at the top; rendered at the end of the script so main tables render on screen FIRST.
frm_container = st.container()

st.markdown("<br>", unsafe_allow_html=True)

# ============================================================
# --- Daily Readings and Imbalances Grid ---
# Builds the main scrollable HTML table that shows:
#   - Calendar date, daily MMBtu imbalance, cumulative MMBtu
#   - Meter readings per meter (Refinery Usage)
#   - Burn Total MMBtu
#   - Fuel Usage (volume + cost per pipeline)
#   - Total $ (sum of fuel costs)
#   - FIRM TRANSPORT (FIRM / IT / USAGE for El Dorado; single type for others)
# ============================================================

# --- Compute Daily Total Buy ---
# DAILY_TOTAL_BUY is the sum of all supplier volumes for a given day.
# Used as the USAGE volume in the FIRM TRANSPORT calculation for El Dorado.
# Refinery Imbalance, Fuel Usage and Meter Readings
# Use DAILY_TOTAL_BUY from DB if present, else calculate
if not df_filtered.empty:
    dtb_col = next((c for c in df_filtered.columns if str(c).upper() == 'DAILY_TOTAL_BUY'), None)
    if dtb_col:
        daily_buy_totals = df_filtered[['DATE_PARSED', dtb_col]].drop_duplicates(subset=['DATE_PARSED'])
        daily_buy_totals = daily_buy_totals.rename(columns={dtb_col: 'DAILY_TOTAL_BUY'})
    else:
        daily_buy_totals = df_filtered.groupby(['DATE_PARSED'])['VOLUME'].sum().reset_index()
        daily_buy_totals = daily_buy_totals.rename(columns={'VOLUME': 'DAILY_TOTAL_BUY'})
else:
    daily_buy_totals = pd.DataFrame(columns=['DATE_PARSED', 'DAILY_TOTAL_BUY'])

# --- Extract Unique Meters ---
# Builds an ordered list of meter names and a meter name → meter number map
# to use as column headers in the readings grid.
if not df_readings_filtered.empty:
    if 'METER_NAME' in df_readings_filtered.columns:
        meters = df_readings_filtered['METER_NAME'].dropna().unique().tolist()
    else:
        meters = []
        
    meters = sorted([str(m) for m in meters])
    
    meter_no_col = next((c for c in df_readings_filtered.columns if 'NUMBER' in str(c).upper() or 'NO' in str(c).upper() and 'METER' in str(c).upper()), None)
    meter_number_map = {}
    if meter_no_col:
        for _, row in df_readings_filtered[['METER_NAME', meter_no_col]].dropna().drop_duplicates().iterrows():
            meter_number_map[str(row['METER_NAME'])] = str(row[meter_no_col])
else:
    meters = []
    meter_number_map = {}
    meter_no_col = None


# --- Daily Readings and Imbalances ---
hdr_title_col, hdr_btn_col = st.columns([8.4, 1.6])

with hdr_btn_col:
    stage_info = get_stage_dir_and_prefix(selected_period, selected_refinery)
    current_source_context = (selected_period, selected_refinery)
 
    # Filters changed since last fetch -> drop whatever we had on display
    if st.session_state.get("source_file_context") != current_source_context:
        st.session_state.pop("source_file_bytes", None)
        st.session_state.pop("source_file_name", None)
        st.session_state.pop("source_file_error", None)
        st.session_state.pop("source_file_context", None)
 
    if stage_info:
        stage_dir, file_prefix, yyyymmdd = stage_info
        st.markdown("""
            <style>
            div[data-testid="stColumn"] div[data-testid="stButton"] > button:not(:disabled),
            div[data-testid="stColumn"] div[data-testid="stDownloadButton"] > button:not(:disabled),
            .stButton > button:not(:disabled),
            .stDownloadButton > button:not(:disabled) {
                background-color: #00843d !important;
                background: #00843d !important;
                color: #ffffff !important;
                border: none !important;
                font-weight: 600 !important;
                box-shadow: none !important;
                outline: none !important;
            }
            div[data-testid="stColumn"] div[data-testid="stButton"] > button:not(:disabled):hover,
            div[data-testid="stColumn"] div[data-testid="stDownloadButton"] > button:not(:disabled):hover,
            div[data-testid="stColumn"] div[data-testid="stButton"] > button:not(:disabled):focus,
            div[data-testid="stColumn"] div[data-testid="stDownloadButton"] > button:not(:disabled):focus,
            div[data-testid="stColumn"] div[data-testid="stButton"] > button:not(:disabled):active,
            div[data-testid="stColumn"] div[data-testid="stDownloadButton"] > button:not(:disabled):active,
            .stButton > button:not(:disabled):hover,
            .stDownloadButton > button:not(:disabled):hover,
            .stButton > button:not(:disabled):focus,
            .stDownloadButton > button:not(:disabled):focus,
            .stButton > button:not(:disabled):active,
            .stDownloadButton > button:not(:disabled):active {
                background-color: #00843d !important;
                background: #00843d !important;
                color: #ffffff !important;
                border: none !important;
                box-shadow: none !important;
                outline: none !important;
            }
            div[data-testid="stColumn"] div[data-testid="stButton"] > button:not(:disabled) p,
            div[data-testid="stColumn"] div[data-testid="stDownloadButton"] > button:not(:disabled) p,
            .stButton > button:not(:disabled) p,
            .stDownloadButton > button:not(:disabled) p {
                color: #ffffff !important;
            }
            div[data-testid="stButton"] > button:disabled,
            .stButton > button:disabled {
                background: transparent !important;
                background-color: transparent !important;
                border: 1px solid #cccccc !important;
                border-radius: 6px !important;
                box-shadow: none !important;
            }
            div[data-testid="stButton"] > button:disabled p,
            .stButton > button:disabled p {
                background: transparent !important;
                background-color: transparent !important;
                border: none !important;
                color: #cccccc !important;
            }
            </style>
            <div style='margin-top: 4px;'></div>
        """, unsafe_allow_html=True)
        if st.button("Get Source File", key="fetch_source_file_btn", use_container_width=True):
            conn = st.connection("snowflake")
            session = conn.session()
            data, resolved_name, err = download_stage_file_bytes(session, stage_dir, file_prefix, yyyymmdd)
            st.session_state["source_file_context"] = current_source_context
            if err:
                st.session_state["source_file_error"] = err
                st.session_state.pop("source_file_bytes", None)
                st.session_state.pop("source_file_name", None)
            else:
                st.session_state["source_file_bytes"] = data
                st.session_state["source_file_name"] = resolved_name
                st.session_state.pop("source_file_error", None)
 
with hdr_title_col:
    st.markdown(
        "<h3 style='color: #38A169; font-size: 1.25rem; margin-bottom: 0;'>Daily Readings and Imbalances</h3>",
        unsafe_allow_html=True
    )
 
if st.session_state.get("source_file_error"):
    st.error(f"Could not fetch source file: {st.session_state['source_file_error']}")
elif st.session_state.get("source_file_bytes"):
    resolved_name = st.session_state["source_file_name"]
    ext = os.path.splitext(resolved_name)[1].lower()
    MIME_MAP = {
        ".pdf": "application/pdf",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }
    st.download_button(
        label=f"⬇ Download {resolved_name}",
        data=st.session_state["source_file_bytes"],
        file_name=resolved_name,
        mime=MIME_MAP.get(ext, "application/octet-stream"),
        key="download_source_file_btn"
    )

    
# --- Edit Meter Readings Expander ---
# Disabled (read-only info message) when the period status is 'Closed'.
# When open, presents a data_editor for editing DAILY_BURN_READING per meter per day.
# Save writes the change via SCD-style UPDATE + INSERT into NG_REFINERY_METER_READING.
@st.fragment
def render_edit_meter_readings_section(status_lower, has_edit_access, df_readings_filtered, meters, meter_number_map):
    if st.session_state.get("show_mr_toast"):
        st.toast(st.session_state.show_mr_toast)
        del st.session_state["show_mr_toast"]

    with st.expander("Edit Meter Readings"):
        if "closed" in status_lower:
            st.info("Editing is disabled for closed periods.")
        elif not has_edit_access:
            st.info("You do not have permission to edit this refinery's data.")
        elif not df_readings_filtered.empty:
            st.write("Edit meter readings for the selected period:")
            
            # Prepare data for editor
            unique_dates = df_readings_filtered[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().dropna().sort_values('DATE_PARSED')
            
            meter_name_col = 'METER_NAME'
            burn_col = 'DAILY_BURN_READING'
            
            edit_records = []
            for _, date_row in unique_dates.iterrows():
                dt = date_row['DATE_PARSED']
                day_str = date_row['DISPLAY_DAY']
                record = {"Date": day_str, "_DATE_PARSED": dt}
                day_data = df_readings_filtered[df_readings_filtered['DATE_PARSED'] == dt]
                for meter in meters:
                    meter_val = day_data[day_data[meter_name_col] == meter][burn_col].sum()
                    record[meter] = int(round(float(meter_val))) if pd.notnull(meter_val) else 0
                edit_records.append(record)
                
            edit_df = pd.DataFrame(edit_records)
            edit_df["_spacer"] = ""  # narrow spacer so the last data column clears the scrollbar
            
            column_config = {"Date": st.column_config.Column("Date", disabled=True, width="medium"), "_DATE_PARSED": None}
            for meter in meters:
                column_config[meter] = st.column_config.NumberColumn(meter, required=True, step=1, format="%d", width="medium")
            column_config["_spacer"] = st.column_config.Column(" ", disabled=True, width=14)
                
            st.markdown("""
<style>
/* Reserve a permanent gutter for the scrollbar so it never overlaps
   cell content or the drag/fill handle in the last column */
[data-testid="stDataEditor"] [data-testid="glideDataEditor"] .dvn-scroller {
    scrollbar-gutter: stable !important;
    padding-right: 4px !important;
}
</style>
""", unsafe_allow_html=True)

            edited_df = st.data_editor(
                edit_df,
                column_config=column_config,
                hide_index=True,
                use_container_width=True,
                height=390,
                key="edit_meter_readings_editor"
            )
            
            if "confirm_changes_key_counter_mr" not in st.session_state:
                st.session_state.confirm_changes_key_counter_mr = 0
            chk_key = f"confirm_changes_mr_{st.session_state.confirm_changes_key_counter_mr}"
            
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("""
<style>
button[kind="primary"] {
    min-height: 32px !important;
    height: 32px !important;
    padding-top: 0px !important;
    padding-bottom: 0px !important;
}
</style>
""", unsafe_allow_html=True)
            chk_col, save_col, _ = st.columns([2.2, 1.3, 6.4])
            with chk_col:
                confirm_changes = st.checkbox("Confirm Changes", key=chk_key)
                
            with save_col:
                if st.button("Save", type="primary", disabled=not confirm_changes, key="save_mr_btn"):
                    with st.spinner("Saving Meter Readings..."):
                        changes_made = False
                        try:
                            conn = st.connection("snowflake")
                            session = conn.session()
                            # RECORD_START_TS = first day of selected period month
                            try:
                                _p_dt = pd.to_datetime(selected_period, errors='coerce')
                                current_ts = _p_dt.replace(day=1).strftime('%Y-%m-%d') if _p_dt is not None and not pd.isna(_p_dt) else pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                            except Exception:
                                current_ts = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                            
                            try:
                                if hasattr(st, "user") and hasattr(st.user, "user_name") and st.user.user_name:
                                    username = st.user.user_name
                                else:
                                    username = "streamlit user"
                            except Exception:
                                username = "streamlit user"
                            
                            updates_sql_list = []
                            inserts_val_list = []

                            for idx, row in edit_df.iterrows():
                                for meter in meters:
                                    old_val = row[meter]
                                    new_val = edited_df.iloc[idx][meter]
                                    
                                    if old_val != new_val:
                                        changes_made = True
                                        dt_parsed = row["_DATE_PARSED"]
                                        cal_date_str = pd.to_datetime(dt_parsed).strftime('%Y-%m-%d')
                                        
                                        meter_data = df_readings_filtered[
                                            (df_readings_filtered['DATE_PARSED'] == dt_parsed) & 
                                            (df_readings_filtered[meter_name_col] == meter)
                                        ]
                                        
                                        ref_id = meter_data['REFINERY_ID'].iloc[0] if not meter_data.empty and 'REFINERY_ID' in meter_data.columns else 0
                                        try: ref_id = int(ref_id)
                                        except: ref_id = 0
                                        
                                        m_num = meter_data['METER_NUMBER'].iloc[0] if not meter_data.empty and 'METER_NUMBER' in meter_data.columns else (meter_number_map.get(meter, meter))
                                        
                                        daily_vol = meter_data['DAILY_VOLUME'].iloc[0] if not meter_data.empty and 'DAILY_VOLUME' in meter_data.columns else 0
                                        try: daily_vol = int(daily_vol)
                                        except: daily_vol = 0
                                        
                                        end_ts_expr = get_effective_end_ts_expr(selected_period)
                                        update_sql = f"""
                                            UPDATE CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_METER_READING
                                            SET RECORD_END_TS = {end_ts_expr}
                                            WHERE REFINERY_ID = {ref_id} 
                                              AND METER_NUMBER = '{m_num}'
                                              AND CALENDAR_DATE = '{cal_date_str}'
                                              AND RECORD_END_TS IS NULL
                                        """
                                        updates_sql_list.append(update_sql)
                                        inserts_val_list.append(f"({ref_id}, '{cal_date_str}', '{m_num}', {new_val}, '{current_ts}', NULL, '{username}')")

                            if updates_sql_list or inserts_val_list:
                                all_statements = list(updates_sql_list)
                                if inserts_val_list:
                                    bulk_insert_sql = f"""
                                        INSERT INTO CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_METER_READING
                                        (REFINERY_ID, CALENDAR_DATE, METER_NUMBER, DAILY_BURN_READING, RECORD_START_TS, RECORD_END_TS, UPDATED_BY)
                                        VALUES
                                        {', '.join(inserts_val_list)}
                                    """
                                    all_statements.append(bulk_insert_sql)
                                
                                ref_id_check = get_refinery_id_by_name(selected_refinery)
                                ref_name_upper = str(selected_refinery).strip().upper() if selected_refinery else ""
                                if ref_id_check == 9 or ref_name_upper in ["PARCO"]:
                                    sp_call_parco = get_sp_burn_tracker_volume_call(selected_period, "PARCO")
                                    sp_call_casper = get_sp_burn_tracker_volume_call(selected_period, "CASPER")
                                    if sp_call_parco:
                                        all_statements.append(sp_call_parco)
                                    if sp_call_casper and sp_call_casper != sp_call_parco:
                                        all_statements.append(sp_call_casper)

                                execute_batch_sql(session, all_statements)
                            
                            if changes_made:
                                st.session_state.show_mr_toast = "Successfully updated Meter Readings!"
                                st.session_state.confirm_changes_key_counter_mr += 1
                                st.cache_data.clear()
                                st.rerun()
                            else:
                                st.session_state.show_mr_toast = "No reading changes were made."
                                st.session_state.confirm_changes_key_counter_mr += 1
                                st.rerun()
                                
                        except Exception as e:
                            st.error(f"Error saving to Snowflake: {e}")
        else:
            st.info("No data available to edit.")

render_edit_meter_readings_section(status_lower, has_edit_access, df_readings_filtered, meters, meter_number_map)

if not df_readings_filtered.empty:
    carryover_val = get_carryover_imbalance(selected_period, selected_refinery)
    
    # --- Prepare FUEL columns ---
    if not df_fuel_filtered.empty:
        df_fuel_filtered.columns = [str(c).upper() for c in df_fuel_filtered.columns]
    vol_col = 'R_VOLUME'
    cost_col = 'R_COST'
    pipe_col = 'PIPELINE_NAME'
    rate_col = 'TRANSPORTATION_RATE'
    
    pipelines = sorted([p for p in df_fuel_filtered[pipe_col].dropna().unique() if str(p).strip() != ""]) if pipe_col in df_fuel_filtered.columns else []

    # --- FIRM TRANSPORT data ---
    FT_COLOR = '#565a5c'
    ft_refinery_id = None
    if 'REFINERY_ID' in df_readings_filtered.columns:
        try: ft_refinery_id = int(df_readings_filtered['REFINERY_ID'].dropna().iloc[0])
        except: pass
    if ft_refinery_id is None and not df_filtered.empty and 'REFINERY_ID' in df_filtered.columns:
        try: ft_refinery_id = int(df_filtered['REFINERY_ID'].dropna().iloc[0])
        except: pass

    df_transport = pd.DataFrame()
    if ft_refinery_id and selected_period:
        df_transport = get_transport_contract_data(selected_period, ft_refinery_id)
        if not df_transport.empty:
            df_transport.columns = [str(c).upper() for c in df_transport.columns]
            df_transport['DATE_PARSED'] = pd.to_datetime(df_transport['PRICE_DATE'], errors='coerce')

    is_el_dorado = (ft_refinery_id == 3)
    is_tulsa = (ft_refinery_id in [4, 5])
    ft_contracts = []  # list of dicts: type, contract_num, rate, col_label, df

    if not df_transport.empty:
        if is_el_dorado:
            for ct_type, col_label in [('FIRM', 'Demand'), ('IT', 'Demand'), ('USAGE', 'Commodity')]:
                ct_df = df_transport[df_transport['TR_CONTRACT_TYPE'].str.upper().isin(['FIRM', 'BASE']) if ct_type == 'FIRM' else (df_transport['TR_CONTRACT_TYPE'].str.upper() == ct_type)].copy()
                contract_num = str(ct_df['TR_CONTRACT_NUMBER'].iloc[0]).strip() if not ct_df.empty else ct_type
                rate_vals = ct_df['BASE_RATE'].dropna()
                rate = float(rate_vals.iloc[0]) if not rate_vals.empty else 0.0
                ft_contracts.append({'type': ct_type, 'contract_num': contract_num, 'rate': rate, 'col_label': col_label, 'df': ct_df})
        elif is_tulsa:
            for ct_type, col_label in [('FIRM', 'Demand'), ('USAGE', 'Commodity')]:
                ct_df = df_transport[df_transport['TR_CONTRACT_TYPE'].str.upper().isin(['FIRM', 'BASE']) if ct_type == 'FIRM' else (df_transport['TR_CONTRACT_TYPE'].str.upper() == ct_type)].copy()
                contract_num = str(ct_df['TR_CONTRACT_NUMBER'].iloc[0]).strip() if not ct_df.empty else ct_type
                rate_vals = ct_df['BASE_RATE'].dropna()
                rate = float(rate_vals.iloc[0]) if not rate_vals.empty else 0.0
                ft_contracts.append({'type': ct_type, 'contract_num': contract_num, 'rate': rate, 'col_label': col_label, 'df': ct_df})
        else:
            def contract_sort_key(ct):
                u = str(ct).strip().upper()
                if u in ['BASE', 'FIRM']: return 0
                if u == 'IT': return 1
                if u == 'USAGE': return 2
                return 3

            sorted_types = [t for t in sorted(df_transport['TR_CONTRACT_TYPE'].str.upper().unique(), key=contract_sort_key) if t != 'IT']
            for ct_type in sorted_types:
                ct_df = df_transport[df_transport['TR_CONTRACT_TYPE'].str.upper() == ct_type].copy()
                contract_num = str(ct_df['TR_CONTRACT_NUMBER'].iloc[0]).strip() if not ct_df.empty else ''
                rate_vals = ct_df['BASE_RATE'].dropna()
                rate = float(rate_vals.iloc[0]) if not rate_vals.empty else 0.0
                col_label = 'Demand' if ct_type in ['BASE', 'FIRM', 'IT'] else ('Commodity' if ct_type == 'USAGE' else '')
                ft_contracts.append({'type': ct_type, 'contract_num': contract_num, 'rate': rate, 'col_label': col_label, 'df': ct_df})

        # Ensure column order is always: Firm -> IT -> Usage
        ft_contracts = sorted(
            ft_contracts,
            key=lambda fc: (0 if str(fc['type']).upper() in ['BASE', 'FIRM'] else (1 if str(fc['type']).upper() == 'IT' else (2 if str(fc['type']).upper() == 'USAGE' else 3)))
        )

    ft_total_cols = len(ft_contracts) * 2

    html = []
    html.append("<style>")
    html.append(f".st-grid-wrapper {{ border: 1px solid #d1d2d5; border-radius: 4px; overflow: auto; max-height: 390px; position: relative; background-color: #ffffff; }}")
    html.append(f".st-grid {{ border-collapse: collapse; border-spacing: 0; font-family: inherit; font-size: 14px; white-space: nowrap; color: #31333F; background-color: #ffffff; width: 100%; text-align: center; }}")
    html.append("""
    .st-grid th, .st-grid td { border: 1px solid #d1d2d5; padding: 4px 6px; font-family: inherit; font-size: 14px; color: #31333F; }
    .st-grid th { font-size: 12px !important; }
    .st-grid td { text-align: right; }
    .st-grid td.sticky-col-0 { text-align: center; }
    .st-grid thead { position: sticky; top: 0; z-index: 10; background-color: #ffffff; }
    .st-grid .sticky-col { position: sticky !important; z-index: 6; background-color: #ffffff; }
    .st-grid .sticky-col-0 { left: 0; min-width: 100px; max-width: 100px; }
    .st-grid .sticky-col-1 { left: 100px; min-width: 110px; max-width: 110px; }
    .st-grid .sticky-col-2 { left: 210px; min-width: 140px; max-width: 140px; border-right: none !important; box-shadow: inset -3px 0 0 0 #bebebe; }
    .st-grid thead .sticky-col { z-index: 15; background-color: #ffffff; }
    </style>""")
    html.append("<div class='st-grid-wrapper'>")
    html.append("<table class='st-grid'>")
    html.append("<thead>")
    
    is_artesia_filter = (selected_refinery and str(selected_refinery).strip().upper() == "ARTESIA")
    prev_imb_rdu = get_carryover_imbalance(selected_period, "ARTESIA RDU") if is_artesia_filter else 0.0
    prev_imb_artesia = carryover_val if is_artesia_filter else 0.0

    # Row 1
    html.append("<tr>")
    html.append("<th class='sticky-col' colspan='2' style='left: 0; font-weight: 700; font-size: 12px; background-color: #013e72; color: white;'>Previous Month Imbalance<br>(MMBtu)</th>")
    html.append(f"<th class='sticky-col' colspan='1' style='left: 210px; font-weight: 700; font-size: 14px; background-color: #013e72; color: white;'>{carryover_val:,.2f}</th>")
    html.append(f"<th colspan='{len(meters) + 1}' style='background: #004438; color: white; font-weight: 700; font-size: 13px; min-width: 300px;'>Refinery Usage</th>")
    html.append("<th rowspan='4' style='background: #bebebe; color: white; font-weight: 700; font-size: 13px; min-width: 100px; vertical-align: middle; text-align: center; border: 1px solid #d1d2d5;'>Total Buy<br>Volume<br>(MMBtu)</th>")
    if is_artesia_filter:
        html.append(f"<th rowspan='4' style='background: #bebebe; color: white; font-weight: 700; font-size: 13px; min-width: 98px; vertical-align: middle; text-align: center; border: 1px solid #d1d2d5;'>Artesia Burn<br>Total<br>(MMBtu)</th>")
        html.append(f"<th rowspan='4' style='background: #bebebe; color: white; font-weight: 700; font-size: 13px; min-width: 98px; vertical-align: middle; text-align: center; border: 1px solid #d1d2d5;'>RDU Burn<br>Total<br>(MMBtu)</th>")
        html.append(f"<th colspan='2' style='background: #013e72; color: white; font-weight: 700; font-size: 14px; text-align: center; border: 1px solid #d1d2d5;'>Artesia</th>")
        html.append(f"<th colspan='2' style='background: #013e72; color: white; font-weight: 700; font-size: 14px; text-align: center; border: 1px solid #d1d2d5;'>Artesia RDU</th>")
    if pipelines:
        html.append(f"<th colspan='{len(pipelines) * 2}' style='background: #cd8a2a; color: white; font-weight: 700; font-size: 14px;'>Fuel Usage</th>")
        html.append(f"<th rowspan='4' style='background: #cd8a2a; color: white; vertical-align: middle; font-weight: 700; font-size: 14px; min-width: 100px;'>Total $</th>")
    if ft_total_cols > 0:
        html.append(f"<th colspan='{ft_total_cols}' style='background: {FT_COLOR}; color: white; font-weight: 700; font-size: 13px;'>FIRM Transport</th>")
    html.append("</tr>")
    
    # Row 2
    html.append("<tr>")
    html.append("<th class='sticky-col sticky-col-0' rowspan='3' style='font-weight: 700; background-color: #013e72; color: white;'>Calendar Date</th>")
    html.append("<th class='sticky-col sticky-col-1' rowspan='3' style='font-weight: 700; background-color: #013e72; color: white;'>Daily Imbalance<br>(MMBtu)</th>")
    html.append("<th class='sticky-col sticky-col-2' rowspan='3' style='font-weight: 700; background-color: #013e72; color: white;'>Cumulative Imbalance<br>(MMBtu)</th>")
    for meter in meters:
        m_num = meter_number_map.get(meter, "")
        label = f"{meter.title()}<br>{m_num}<br>Volume (MMBtu)" if m_num else f"{meter.title()}<br>Volume (MMBtu)"
        html.append(f"<th rowspan='3' style='background: #004438; color: white; font-weight: 600; min-width: 140px;'>{label}</th>")
    html.append(f"<th rowspan='3' style='background: #004438; color: white; font-weight: 600; min-width: 120px;'>Burn Total<br>(MMBtu)</th>")
    
    if is_artesia_filter:
        html.append(f"<th colspan='1' style='background: #013e72; color: white; font-weight: 700; font-size: 12px; text-align: left; border: 1px solid #d1d2d5;'>Previous Imbalance</th>")
        html.append(f"<th colspan='1' style='background: #013e72; color: white; font-weight: 700; font-size: 14px; text-align: right; border: 1px solid #d1d2d5;'>{prev_imb_artesia:,.2f}</th>")
        html.append(f"<th colspan='1' style='background: #013e72; color: white; font-weight: 700; font-size: 12px; text-align: left; border: 1px solid #d1d2d5;'>Previous Imbalance</th>")
        html.append(f"<th colspan='1' style='background: #013e72; color: white; font-weight: 700; font-size: 14px; text-align: right; border: 1px solid #d1d2d5;'>{prev_imb_rdu:,.2f}</th>")
    for i, pipe in enumerate(pipelines):
        html.append(f"<th colspan='2' style='background: #cd8a2a; color: white; font-weight: 600;'>{pipe}</th>")
    for fc in ft_contracts:
        t_upper = str(fc['type']).strip().upper()
        if t_upper == 'FIRM':
            disp_type = 'Firm'
        elif t_upper == 'BASE':
            disp_type = 'Firm'
        elif t_upper == 'USAGE':
            disp_type = 'Usage'
        elif t_upper == 'IT':
            disp_type = 'IT'
        else:
            disp_type = str(fc['type']).title()
        html.append(f"<th colspan='2' style='background: {FT_COLOR}; color: white; font-weight: 600;'>{disp_type}</th>")
    html.append("</tr>")
    
    # Row 3 (Rates)
    html.append("<tr>")
    if is_artesia_filter:
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>Daily Imbalance</th>")
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>Cumulative Imbalance</th>")
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>Daily Imbalance</th>")
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>Cumulative Imbalance</th>")
    for pipe in pipelines:
        pipe_data = df_fuel_filtered[df_fuel_filtered[pipe_col] == pipe]
        
        if rate_col and rate_col in pipe_data.columns and not pipe_data[rate_col].empty:
            rate = float(pipe_data[rate_col].iloc[0])
        else:
            total_vol = pipe_data[vol_col].sum() if vol_col in pipe_data.columns else 0
            total_cost = pipe_data[cost_col].sum() if cost_col in pipe_data.columns else 0
            rate = total_cost / total_vol if total_vol != 0 else 0
        
        html.append(f"<th style='background: #cd8a2a; color: white; font-weight: 600; min-width: 95px;'>Rate</th>")
        html.append(f"<th style='background: #cd8a2a; color: white; font-weight: 600; min-width: 95px;'>{rate:.6f}</th>")
    for fc in ft_contracts:
        html.append(f"<th style='background: {FT_COLOR}; color: white; font-weight: 600; min-width: 95px;'>{fc['col_label']}</th>")
        html.append(f"<th style='background: {FT_COLOR}; color: white; font-weight: 600; min-width: 95px;'>{fc['contract_num']}</th>")
    html.append("</tr>")
    
    # Row 4 (Vol/Cost)
    html.append("<tr>")
    if is_artesia_filter:
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>(MMBtu)</th>")
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>(MMBtu)</th>")
        html.append(f"<th style='background: #013e72; color: white; font-weight: 600; text-align: center; border: 1px solid #d1d2d5;'>(MMBtu)</th>")
        html.append(f"<th style='background: #013e72; color: white; font-weight: 700; font-size: 14px; text-align: center; border: 1px solid #d1d2d5;'>(MMBtu)</th>")
    for pipe in pipelines:
        html.append("<th style='background: #cd8a2a; color: white; font-weight: 600; min-width: 95px;'>Volume</th>")
        html.append("<th style='background: #cd8a2a; color: white; font-weight: 600; min-width: 95px;'>Cost</th>")
    for fc in ft_contracts:
        html.append(f"<th style='background: {FT_COLOR}; color: white; font-weight: 600; min-width: 95px;'>Volume</th>")
        html.append(f"<th style='background: {FT_COLOR}; color: white; font-weight: 600; min-width: 95px;'>{fc['rate']:.5f}</th>")
    html.append("</tr>")
    
    html.append("</thead>")
    html.append("<tbody>")
    
    # Merge daily_buy_totals with unique dates from readings
    unique_dates = df_readings_filtered[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().dropna().sort_values('DATE_PARSED')
    unique_dates = pd.merge(unique_dates, daily_buy_totals, on='DATE_PARSED', how='left')
    unique_dates['DAILY_TOTAL_BUY'] = unique_dates['DAILY_TOTAL_BUY'].fillna(0)
    
    # --- Burn Total (MMBtu) calculation for Refinery Usage ---
    display_burn_totals = get_display_burn_totals(df_readings_filtered, selected_refinery)
    is_parco_table = is_parco_refinery(selected_refinery, df_readings_filtered)

    cumulative_mmbtu = carryover_val
    
    # Tracking totals for the final row
    sum_daily_mmbtu = 0
    sum_meters = {m: 0 for m in meters}
    sum_burn_total = 0
    sum_total_buy = 0
    sum_fuel_vols = {p: 0 for p in pipelines}
    sum_fuel_costs = {p: 0 for p in pipelines}
    sum_overall_cost = 0
    sum_ft_vols = {fc['type']: 0.0 for fc in ft_contracts}
    sum_ft_costs = {fc['type']: 0.0 for fc in ft_contracts}

    if is_artesia_filter:
        cum_artesia = prev_imb_artesia
        cum_rdu = prev_imb_rdu
        sum_artesia_burn = 0.0
        sum_rdu_burn = 0.0
        sum_daily_imb_artesia = 0.0
        sum_daily_imb_rdu = 0.0

        refinery_col = "REFINERY_NAME" if "REFINERY_NAME" in df_raw.columns else "REFINERY"
        df_rdu_raw = df_raw[df_raw[refinery_col].astype(str).str.strip().str.upper() == "ARTESIA RDU"] if not df_raw.empty and refinery_col in df_raw.columns else pd.DataFrame()
    
    for r_idx, (_, date_row) in enumerate(unique_dates.iterrows()):
        day_str = date_row['DISPLAY_DAY']
        dt = date_row['DATE_PARSED']
        daily_total_buy = float(date_row['DAILY_TOTAL_BUY']) if pd.notnull(date_row['DAILY_TOTAL_BUY']) else 0.0
        
        # Readings data
        day_data = df_readings_filtered[df_readings_filtered['DATE_PARSED'] == dt]
        meter_cells = []
        for meter in meters:
            meter_val = float(day_data[day_data['METER_NAME'] == meter]['DAILY_BURN_READING'].sum())
            sum_meters[meter] += meter_val
            cell_val = f"{meter_val:,.0f}" if meter_val != 0 else "0"
            meter_cells.append(f"<td>{cell_val}</td>")
            
        daily_total_burn = calculate_daily_burn_sum(day_data, is_parco=is_parco_table)
        display_burn = display_burn_totals[r_idx] if r_idx < len(display_burn_totals) else daily_total_burn

        # Fuel usage volume for the day (safely 0.0 if fuel section/data is missing)
        daily_fuel_vol = 0.0
        fuel_date_data = df_fuel_filtered[df_fuel_filtered['DATE_PARSED'] == dt] if (not df_fuel_filtered.empty and 'DATE_PARSED' in df_fuel_filtered.columns) else pd.DataFrame()
        if pipelines and not fuel_date_data.empty:
            for pipe in pipelines:
                pipe_date_data = fuel_date_data[fuel_date_data[pipe_col] == pipe]
                if not pipe_date_data.empty and vol_col in pipe_date_data.columns:
                    v_raw = pipe_date_data[vol_col].iloc[0]
                    try:
                        v_val = float(v_raw) if pd.notna(v_raw) and v_raw is not None else 0.0
                    except Exception:
                        v_val = 0.0
                    daily_fuel_vol += v_val

        daily_mmbtu = daily_total_buy - display_burn - daily_fuel_vol
        cumulative_mmbtu += daily_mmbtu
        
        sum_daily_mmbtu += daily_mmbtu
        sum_burn_total += display_burn
        sum_total_buy += daily_total_buy
        
        html.append("<tr>")
        html.append(f"<td class='sticky-col sticky-col-0'>{day_str}</td>")
        html.append(f"<td class='sticky-col sticky-col-1'>{daily_mmbtu:,.2f}</td>")
        html.append(f"<td class='sticky-col sticky-col-2'>{cumulative_mmbtu:,.2f}</td>")
        
        html.extend(meter_cells)
        html.append(f"<td>{display_burn:,.0f}</td>")
        html.append(f"<td>{daily_total_buy:,.2f}</td>")

        if is_artesia_filter:
            rdu_mask = pd.Series(False, index=day_data.index)
            if 'METER_NAME' in day_data.columns:
                rdu_mask |= day_data['METER_NAME'].astype(str).str.strip().str.upper().str.contains('3390|HEP-RDU|RDU-HEP|HEP RDU|RDU HEP|HEP_RDU')
            if 'METER_NUMBER' in day_data.columns:
                rdu_mask |= day_data['METER_NUMBER'].astype(str).str.strip().str.upper().str.contains('3390')
            rdu_hep_rows = day_data[rdu_mask] if not day_data.empty else pd.DataFrame()
            rdu_burn_val = float(rdu_hep_rows['DAILY_BURN_READING'].sum()) if not rdu_hep_rows.empty and 'DAILY_BURN_READING' in rdu_hep_rows.columns else 0.0
            artesia_burn_val = display_burn - rdu_burn_val

            rdu_day_raw = df_rdu_raw[df_rdu_raw['DATE_PARSED'] == dt] if not df_rdu_raw.empty and 'DATE_PARSED' in df_rdu_raw.columns else pd.DataFrame()
            rdu_buy_vol = float(rdu_day_raw['VOLUME'].sum()) if not rdu_day_raw.empty and 'VOLUME' in rdu_day_raw.columns else 0.0

            artesia_buy_vol = daily_total_buy - rdu_buy_vol

            total_rdu_fuel_ded = 0.0
            total_artesia_fuel_ded = 0.0
            if pipelines:
                for pipe in pipelines:
                    pipe_date_data = fuel_date_data[fuel_date_data[pipe_col] == pipe] if not fuel_date_data.empty else pd.DataFrame()
                    if not pipe_date_data.empty and rate_col in pipe_date_data.columns and pd.notna(pipe_date_data[rate_col].iloc[0]):
                        try:
                            pipe_rate = float(pipe_date_data[rate_col].iloc[0])
                        except Exception:
                            pipe_rate = 0.0
                    else:
                        pipe_data = df_fuel_filtered[df_fuel_filtered[pipe_col] == pipe] if not df_fuel_filtered.empty else pd.DataFrame()
                        if not pipe_data.empty and rate_col in pipe_data.columns and pd.notna(pipe_data[rate_col].iloc[0]):
                            try:
                                pipe_rate = float(pipe_data[rate_col].iloc[0])
                            except Exception:
                                pipe_rate = 0.0
                        else:
                            total_v = pipe_data[vol_col].sum() if not pipe_data.empty and vol_col in pipe_data.columns else 0.0
                            total_c = pipe_data[cost_col].sum() if not pipe_data.empty and cost_col in pipe_data.columns else 0.0
                            pipe_rate = total_c / total_v if total_v != 0 else 0.0
                    
                    total_rdu_fuel_ded += rdu_buy_vol * pipe_rate
                    total_artesia_fuel_ded += artesia_buy_vol * pipe_rate

            daily_imb_art = artesia_buy_vol - artesia_burn_val - total_artesia_fuel_ded
            daily_imb_rdu = rdu_buy_vol - rdu_burn_val - total_rdu_fuel_ded

            cum_artesia += daily_imb_art
            cum_rdu += daily_imb_rdu

            sum_artesia_burn += artesia_burn_val
            sum_rdu_burn += rdu_burn_val
            sum_daily_imb_artesia += daily_imb_art
            sum_daily_imb_rdu += daily_imb_rdu

            html.append(f"<td>{artesia_burn_val:,.0f}</td>")
            html.append(f"<td>{rdu_burn_val:,.0f}</td>")
            html.append(f"<td>{daily_imb_art:,.2f}</td>")
            html.append(f"<td>{cum_artesia:,.2f}</td>")
            html.append(f"<td>{daily_imb_rdu:,.2f}</td>")
            html.append(f"<td>{cum_rdu:,.2f}</td>")
        
        # Fuel data
        row_total_cost = 0.0
        for pipe in pipelines:
            pipe_date_data = fuel_date_data[fuel_date_data[pipe_col] == pipe]
            if not pipe_date_data.empty and vol_col in pipe_date_data.columns:
                v_raw = pipe_date_data[vol_col].iloc[0]
                try: vol = float(v_raw) if pd.notna(v_raw) and v_raw is not None else 0.0
                except Exception: vol = 0.0
            else:
                vol = 0.0
                
            if not pipe_date_data.empty and cost_col in pipe_date_data.columns:
                c_raw = pipe_date_data[cost_col].iloc[0]
                try: cost = float(c_raw) if pd.notna(c_raw) and c_raw is not None else 0.0
                except Exception: cost = 0.0
            else:
                cost = 0.0
            row_total_cost += cost
            
            sum_fuel_vols[pipe] += vol
            sum_fuel_costs[pipe] += cost
            
            html.append(f"<td>{vol:,.2f}</td>")
            cost_str = f"$({abs(cost):,.2f})" if cost < 0 else f"${cost:,.2f}"
            html.append(f"<td>{cost_str}</td>")
            
        if pipelines:
            sum_overall_cost += row_total_cost
            row_tot_str = f"$({abs(row_total_cost):,.2f})" if row_total_cost < 0 else f"${row_total_cost:,.2f}"
            html.append(f"<td>{row_tot_str}</td>")

        # --- FIRM TRANSPORT per-day cells ---
        for fc in ft_contracts:
            ct_upper = str(fc['type']).upper()
            if ct_upper in ['BASE', 'FIRM']:
                day_tr = fc['df'][fc['df']['DATE_PARSED'].dt.date == pd.Timestamp(dt).date()] if 'DATE_PARSED' in fc['df'].columns else pd.DataFrame()
                ft_vol = float(day_tr['TR_BASE_VOLUME'].iloc[0]) if (not day_tr.empty and 'TR_BASE_VOLUME' in day_tr.columns and pd.notna(day_tr['TR_BASE_VOLUME'].iloc[0])) else 0.0
                ft_cost = float(day_tr['TR_COST'].iloc[0]) if (not day_tr.empty and 'TR_COST' in day_tr.columns and pd.notna(day_tr['TR_COST'].iloc[0])) else 0.0
            elif ct_upper == 'USAGE':
                if is_el_dorado:
                    ft_vol = daily_total_buy
                elif is_tulsa:
                    ft_vol = daily_total_buy - daily_fuel_vol
                else:
                    ft_vol = display_burn
                ft_cost = ft_vol * fc['rate']
            elif ct_upper == 'IT':
                base_fc = next((f for f in ft_contracts if str(f['type']).upper() in ['BASE', 'FIRM']), None)
                if base_fc is not None and 'df' in base_fc and 'DATE_PARSED' in base_fc['df'].columns:
                    day_base = base_fc['df'][base_fc['df']['DATE_PARSED'].dt.date == pd.Timestamp(dt).date()]
                    base_v = float(day_base['TR_BASE_VOLUME'].iloc[0]) if (not day_base.empty and 'TR_BASE_VOLUME' in day_base.columns and pd.notna(day_base['TR_BASE_VOLUME'].iloc[0])) else 0.0
                else:
                    base_v = 0.0
                ft_vol = max(0.0, daily_total_buy - base_v) if is_el_dorado else (daily_total_buy - base_v)
                ft_cost = ft_vol * fc['rate']
            else:
                ft_vol = 0.0
                ft_cost = 0.0
            sum_ft_vols[fc['type']] += ft_vol
            sum_ft_costs[fc['type']] += ft_cost
            ft_cost_str = f"$({abs(ft_cost):,.2f})" if ft_cost < 0 else f"${ft_cost:,.2f}"
            html.append(f"<td>{ft_vol:,.2f}</td>")
            html.append(f"<td>{ft_cost_str}</td>")

        html.append("</tr>")
        
    # --- Add Totals Row ---
    html.append("<tr style='font-weight: bold;'>")
    html.append("<td class='sticky-col sticky-col-0' style='background: #ffffff;'>Total</td>")
    html.append(f"<td class='sticky-col sticky-col-1' style='background: #ffffff;'>{sum_daily_mmbtu:,.2f}</td>")
    html.append(f"<td class='sticky-col sticky-col-2' style='background: #ffffff;'>{cumulative_mmbtu:,.2f}</td>")
    
    for meter in meters:
        html.append(f"<td>{sum_meters[meter]:,.0f}</td>")
    html.append(f"<td>{sum_burn_total:,.0f}</td>")
    html.append(f"<td>{sum_total_buy:,.2f}</td>")
    if is_artesia_filter:
        html.append(f"<td>{sum_artesia_burn:,.0f}</td>")
        html.append(f"<td>{sum_rdu_burn:,.0f}</td>")
        html.append(f"<td>{sum_daily_imb_artesia:,.2f}</td>")
        html.append(f"<td>{cum_artesia:,.2f}</td>")
        html.append(f"<td>{sum_daily_imb_rdu:,.2f}</td>")
        html.append(f"<td>{cum_rdu:,.2f}</td>")
    
    for pipe in pipelines:
        pipe_cost = sum_fuel_costs[pipe]
        pipe_cost_str = f"$({abs(pipe_cost):,.2f})" if pipe_cost < 0 else f"${pipe_cost:,.2f}"
        html.append(f"<td>{sum_fuel_vols[pipe]:,.2f}</td>")
        html.append(f"<td>{pipe_cost_str}</td>")
        
    if pipelines:
        sum_overall_str = f"$({abs(sum_overall_cost):,.2f})" if sum_overall_cost < 0 else f"${sum_overall_cost:,.2f}"
        html.append(f"<td>{sum_overall_str}</td>")
    for fc in ft_contracts:
        ft_cst = sum_ft_costs[fc['type']]
        ft_cst_str = f"$({abs(ft_cst):,.2f})" if ft_cst < 0 else f"${ft_cst:,.2f}"
        html.append(f"<td>{sum_ft_vols[fc['type']]:,.2f}</td>")
        html.append(f"<td>{ft_cst_str}</td>")
    html.append("</tr>")
    
    html.append("</tbody>")
    html.append("</table>")
    html.append("</div>")
    
    st.markdown("".join(html), unsafe_allow_html=True)
else:
    st.info("No meter readings data available for the selected filters.")



# ============================================================
# --- Daily Buy Volume Grid ---
# Renders the buy volume allocation table grouped by:
#   Contract Type → Supplier → Pricing Region
# Shows Buy Volume (MMBtu), All-In Price, and Cost per day.
# Includes an Edit Buy Volume expander with SCD save logic.
# ============================================================

st.markdown("<br>", unsafe_allow_html=True)

st.markdown(f"<h3 style='color: #38A169; font-size: 1.25rem; margin-bottom: 0;'>Daily Buy Volume</h3>", unsafe_allow_html=True)

# Builds and renders the edit form for buy volume changes.
def render_edit_buy_volume(df_supp, key_suffix="t4", title_text="Edit Buy Volume for the selected period:", selected_refinery=None, selected_period=None):
    st.write(title_text)
    if df_supp.empty:
        st.warning("No data available.")
        return

    is_casper = False
    swing_purchase_data = {}
    if selected_refinery:
        ref_name_upper = str(selected_refinery).strip().upper()
        if ref_name_upper == "CASPER" or get_refinery_id_by_name(selected_refinery) == 10:
            is_casper = True
            if selected_period:
                swing_purchase_data = get_swing_purchase_buy_volumes(selected_period)

    unique_dates = df_supp[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().dropna().sort_values('DATE_PARSED')

    options = []
    mapping_dict = {}

    cols_to_group = [c for c in ['CONTRACT_TYPE_NAME', 'SUPPLIER_NAME', 'PRICING_REGION_NAME'] if c in df_supp.columns]

    for _, row in df_supp.iterrows():
        c_type = row.get('CONTRACT_TYPE_NAME', '')
        supp = row.get('SUPPLIER_NAME', '')
        pipe = row.get('PRICING_REGION_NAME', '')
        vol = row.get('VOLUME', 0)
        map_id = row.get('REF_SUP_MAPPING_ID', None)
        cal_date = row.get('CALENDAR_DATE', row.get('DATE_PARSED', None))
        day_str = row.get('DISPLAY_DAY', '')

        label = f"{c_type} | {str(supp).title()} | {str(pipe)}"
        if label not in options:
            options.append(label)

        key = (day_str, label)
        if key not in mapping_dict:
            mapping_dict[key] = {
                'volume': 0.0,
                'map_ids': set(),
                'cal_date': cal_date
            }

        try: v_num = float(vol) if pd.notnull(vol) else 0.0
        except Exception: v_num = 0.0

        mapping_dict[key]['volume'] += v_num
        if map_id is not None and pd.notnull(map_id):
            mapping_dict[key]['map_ids'].add(map_id)
        if mapping_dict[key]['cal_date'] is None and cal_date is not None:
            mapping_dict[key]['cal_date'] = cal_date

    options.sort()
    if options:
        edit_records = []
        for _, date_row in unique_dates.iterrows():
            day_str = date_row['DISPLAY_DAY']
            dt_parsed = date_row.get('DATE_PARSED', None)
            dt_str = pd.to_datetime(dt_parsed).strftime('%Y-%m-%d') if pd.notna(dt_parsed) else ""

            record = {"Date": day_str}
            for label in options:
                cell_data = mapping_dict.get((day_str, label), None)
                if cell_data:
                    try: curr_vol = float(cell_data['volume'])
                    except: curr_vol = 0.0
                else:
                    curr_vol = 0.0
                record[label] = curr_vol

            if is_casper:
                record["Swing Purchase"] = float(swing_purchase_data.get(dt_str, 0.0))

            edit_records.append(record)

        edit_df = pd.DataFrame(edit_records)
        edit_df["_spacer"] = ""  # narrow spacer so the last data column clears the scrollbar

        column_config = {"Date": st.column_config.Column("Date", disabled=True, width="medium")}
        for label in options:
            column_config[label] = st.column_config.NumberColumn(label, required=True, step=0.01, format="%.2f", width="medium")
        if is_casper:
            column_config["Swing Purchase"] = st.column_config.NumberColumn("Swing Purchase", disabled=True, format="%.2f", width="medium")
        column_config["_spacer"] = st.column_config.Column(" ", disabled=True, width=14)
            
        st.markdown("""
<style>
/* Reserve a permanent gutter for the scrollbar so it never overlaps
   cell content or the drag/fill handle in the last column */
[data-testid="stDataEditor"] [data-testid="glideDataEditor"] .dvn-scroller {
    scrollbar-gutter: stable !important;
    padding-right: 4px !important;
}
</style>
""", unsafe_allow_html=True)

        edited_df = st.data_editor(
            edit_df,
            column_config=column_config,
            hide_index=True,
            use_container_width=True,
            height=390,
            key=f"edit_panel_editor_{key_suffix}"
        )
        
        st.markdown("<br>", unsafe_allow_html=True)
        
        chk_counter_key = f"confirm_changes_key_counter_{key_suffix}"
        if chk_counter_key not in st.session_state:
            st.session_state[chk_counter_key] = 0
        chk_key = f"confirm_changes_{key_suffix}_{st.session_state[chk_counter_key]}"
        
        chk_col, save_col, _ = st.columns([2.2, 1.3, 6.4])
        with chk_col:
            confirm_changes = st.checkbox("Confirm Changes", key=chk_key)
            
        with save_col:
            if st.button("Save", type="primary", disabled=not confirm_changes, key=f"save_btn_{key_suffix}"):
                with st.spinner("Saving Buy Volumes..."):
                    changes_made = False
                    try:
                        conn = st.connection("snowflake")
                        session = conn.session()
                        # RECORD_START_TS = first day of selected period month
                        try:
                            _p_dt = pd.to_datetime(selected_period, errors='coerce')
                            current_ts = _p_dt.replace(day=1).strftime('%Y-%m-%d') if _p_dt is not None and not pd.isna(_p_dt) else pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                        except Exception:
                            current_ts = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                        
                        try:
                            if hasattr(st, "user") and hasattr(st.user, "user_name") and st.user.user_name:
                                username = st.user.user_name
                            else:
                                username = "streamlit user"
                        except Exception:
                            username = "streamlit user"
                            
                        updates_sql_list = []
                        inserts_val_list = []

                        for idx, row in edit_df.iterrows():
                            day_str = row["Date"]
                            for label in options:
                                old_vol = row[label]
                                new_vol = float(edited_df.iloc[idx][label])
                                
                                if old_vol != new_vol:
                                    cell_data = mapping_dict.get((day_str, label))
                                    if cell_data and cell_data['map_ids'] and cell_data['cal_date'] is not None:
                                        changes_made = True
                                        map_ids = list(cell_data['map_ids'])
                                        cal_date = cell_data['cal_date']
                                        cal_date_str = pd.to_datetime(cal_date).strftime('%Y-%m-%d')
                                        
                                        end_ts_expr = get_effective_end_ts_expr(selected_period)
                                        for map_id in map_ids:
                                            update_sql = f"""
                                                UPDATE CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME
                                                SET RECORD_END_TS = {end_ts_expr}
                                                WHERE REF_SUP_MAPPING_ID = '{map_id}' 
                                                  AND CALENDAR_DATE = '{cal_date_str}'
                                                  AND RECORD_END_TS IS NULL
                                            """
                                            updates_sql_list.append(update_sql)
                                        
                                        primary_map_id = map_ids[0]
                                        inserts_val_list.append(f"('{primary_map_id}', '{cal_date_str}', {new_vol}, '{current_ts}', NULL, '{username}')")

                        if updates_sql_list or inserts_val_list:
                            all_statements = list(updates_sql_list)
                            if inserts_val_list:
                                bulk_insert_sql = f"""
                                    INSERT INTO CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME
                                    (REF_SUP_MAPPING_ID, CALENDAR_DATE, BUY_VOLUME, RECORD_START_TS, RECORD_END_TS, UPDATED_BY)
                                    VALUES
                                    {', '.join(inserts_val_list)}
                                """
                                all_statements.append(bulk_insert_sql)
                            
                            ref_id_check = get_refinery_id_by_name(selected_refinery)
                            ref_name_upper = str(selected_refinery).strip().upper() if selected_refinery else ""
                            if ref_id_check == 9 or ref_name_upper in ["PARCO"]:
                                sp_call_parco = get_sp_burn_tracker_volume_call(selected_period, "PARCO")
                                sp_call_casper = get_sp_burn_tracker_volume_call(selected_period, "CASPER")
                                if sp_call_parco:
                                    all_statements.append(sp_call_parco)
                                if sp_call_casper and sp_call_casper != sp_call_parco:
                                    all_statements.append(sp_call_casper)
                            else:
                                sp_call = get_sp_burn_tracker_volume_call(selected_period, selected_refinery)
                                if sp_call:
                                    all_statements.append(sp_call)

                            execute_batch_sql(session, all_statements)
                        
                        if changes_made:
                            st.session_state.show_bv_toast = "Successfully updated Buy Volume(s)!"
                            st.session_state[chk_counter_key] += 1
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.session_state.show_bv_toast = "No volume changes were made."
                            st.session_state[chk_counter_key] += 1
                            st.rerun()
                            
                    except Exception as e:
                        st.error(f"Error saving to Snowflake: {e}")

# Builds and renders the specialized edit form for Artesia - RDU buy volume.
def render_edit_buy_volume_artesia_rdu(df_target, artesia_dtb_map, selected_period):
    st.write("Edit Artesia - RDU Buy Volume for the selected period:")
    if df_target.empty:
        st.warning("No data available for Artesia - RDU.")
        return

    unique_dates = df_target[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().dropna().sort_values('DATE_PARSED')
    if unique_dates.empty:
        st.warning("No data available for Artesia - RDU.")
        return

    mapping_dict = {}
    for _, row in df_target.iterrows():
        map_id = row.get('REF_SUP_MAPPING_ID', None)
        cal_date = row.get('CALENDAR_DATE', row.get('DATE_PARSED', None))
        day_str = row.get('DISPLAY_DAY', '')
        dt_parsed = row.get('DATE_PARSED', None)
        vol = row.get('VOLUME', 0)
        try: v_num = float(vol) if pd.notnull(vol) else 0.0
        except Exception: v_num = 0.0

        if day_str not in mapping_dict:
            mapping_dict[day_str] = {
                'volume': 0.0,
                'map_ids': set(),
                'cal_date': cal_date,
                'dt_parsed': dt_parsed
            }

        mapping_dict[day_str]['volume'] += v_num
        if map_id is not None and pd.notnull(map_id):
            mapping_dict[day_str]['map_ids'].add(map_id)
        if mapping_dict[day_str]['cal_date'] is None and cal_date is not None:
            mapping_dict[day_str]['cal_date'] = cal_date

    edit_records = []
    for _, date_row in unique_dates.iterrows():
        day_str = date_row['DISPLAY_DAY']
        dt_parsed = date_row['DATE_PARSED']
        cell_data = mapping_dict.get(day_str, {})
        rdu_vol = float(cell_data.get('volume', 0.0))
        
        # Get Daily Total Buy (MMBtu)
        total_vol = float(artesia_dtb_map.get(dt_parsed, 0.0))
        artesia_vol = total_vol - rdu_vol

        record = {
            "Date": day_str,
            "RDU Volume": rdu_vol,
            "Artesia Volume": artesia_vol,
            "Total Volume": total_vol
        }
        edit_records.append(record)

    edit_df = pd.DataFrame(edit_records)

    column_config = {
        "Date": st.column_config.Column("Date", disabled=True),
        "RDU Volume": st.column_config.NumberColumn("RDU Volume", required=True, step=0.01, format="%.2f"),
        "Artesia Volume": st.column_config.NumberColumn("Artesia Volume", disabled=True, format="%.2f"),
        "Total Volume": st.column_config.NumberColumn("Total Volume", disabled=True, format="%.2f")
    }

    edited_df = st.data_editor(
        edit_df,
        column_config=column_config,
        hide_index=True,
        use_container_width=True,
        height=390,
        key="edit_panel_editor_artesia_rdu"
    )

    # Recalculate Artesia Volume in edited_df for dynamic display if RDU Volume edited
    if "RDU Volume" in edited_df.columns and "Total Volume" in edited_df.columns:
        edited_df["Artesia Volume"] = edited_df["Total Volume"] - edited_df["RDU Volume"]

    st.markdown("<br>", unsafe_allow_html=True)

    chk_counter_key = "confirm_changes_key_counter_artesia_rdu"
    if chk_counter_key not in st.session_state:
        st.session_state[chk_counter_key] = 0
    chk_key = f"confirm_changes_artesia_rdu_{st.session_state[chk_counter_key]}"

    chk_col, save_col, _ = st.columns([2.2, 1.3, 6.4])
    with chk_col:
        confirm_changes = st.checkbox("Confirm Changes", key=chk_key)

    with save_col:
        if st.button("Save", type="primary", disabled=not confirm_changes, key="save_btn_artesia_rdu"):
            with st.spinner("Saving Artesia RDU Buy Volumes..."):
                changes_made = False
                try:
                    conn = st.connection("snowflake")
                    session = conn.session()
                    try:
                        _p_dt = pd.to_datetime(selected_period, errors='coerce')
                        current_ts = _p_dt.replace(day=1).strftime('%Y-%m-%d') if _p_dt is not None and not pd.isna(_p_dt) else pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                    except Exception:
                        current_ts = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S.%f')

                    try:
                        if hasattr(st, "user") and hasattr(st.user, "user_name") and st.user.user_name:
                            username = st.user.user_name
                        else:
                            username = "streamlit user"
                    except Exception:
                        username = "streamlit user"

                    updates_sql_list = []
                    inserts_val_list = []

                    for idx, row in edit_df.iterrows():
                        day_str = row["Date"]
                        old_vol = float(row["RDU Volume"])
                        new_vol = float(edited_df.iloc[idx]["RDU Volume"])

                        if old_vol != new_vol:
                            cell_data = mapping_dict.get(day_str)
                            if cell_data and cell_data['map_ids'] and cell_data['cal_date'] is not None:
                                changes_made = True
                                map_ids = list(cell_data['map_ids'])
                                cal_date = cell_data['cal_date']
                                cal_date_str = pd.to_datetime(cal_date).strftime('%Y-%m-%d')

                                end_ts_expr = get_effective_end_ts_expr(selected_period)
                                for map_id in map_ids:
                                    update_sql = f"""
                                        UPDATE CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME
                                        SET RECORD_END_TS = {end_ts_expr}
                                        WHERE REF_SUP_MAPPING_ID = '{map_id}' 
                                          AND CALENDAR_DATE = '{cal_date_str}'
                                          AND RECORD_END_TS IS NULL
                                    """
                                    updates_sql_list.append(update_sql)

                                primary_map_id = map_ids[0]
                                inserts_val_list.append(f"('{primary_map_id}', '{cal_date_str}', {new_vol}, '{current_ts}', NULL, '{username}')")

                    if updates_sql_list or inserts_val_list:
                        all_statements = list(updates_sql_list)
                        if inserts_val_list:
                            bulk_insert_sql = f"""
                                INSERT INTO CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME
                                (REF_SUP_MAPPING_ID, CALENDAR_DATE, BUY_VOLUME, RECORD_START_TS, RECORD_END_TS, UPDATED_BY)
                                VALUES
                                {', '.join(inserts_val_list)}
                            """
                            all_statements.append(bulk_insert_sql)
                        
                        sp_call = get_sp_burn_tracker_volume_call(selected_period, "ARTESIA RDU")
                        if sp_call:
                            all_statements.append(sp_call)

                        execute_batch_sql(session, all_statements)

                    if changes_made:
                        st.session_state.show_bv_toast = "Successfully updated Buy Volume(s)!"
                        st.session_state[chk_counter_key] += 1
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.session_state.show_bv_toast = "No volume changes were made."
                        st.session_state[chk_counter_key] += 1
                        st.rerun()

                except Exception as e:
                    st.error(f"Error saving to Snowflake: {e}")

@st.fragment
def render_edit_buy_volume_section(status_lower, has_edit_access, selected_suppliers, df_filtered, selected_refinery=None, df_raw=None, df_mapping=None, selected_period=None):
    if st.session_state.get("show_bv_toast"):
        st.toast(st.session_state.show_bv_toast)
        del st.session_state["show_bv_toast"]

    with st.expander("Edit Buy Volume"):
        if "closed" in status_lower:
            st.info("Editing is disabled for closed periods.")
        elif not has_edit_access:
            st.info("You do not have permission to edit this refinery's data.")
        elif selected_suppliers and not df_filtered.empty:
            df_supp = df_filtered[df_filtered['SUPPLIER_NAME'].isin(selected_suppliers)]
            render_edit_buy_volume(df_supp, key_suffix="t4", title_text="Edit Buy Volume for the selected period:", selected_refinery=selected_refinery, selected_period=selected_period)
        else:
            st.warning("Please select suppliers and their mappings.")

    sel_ref_upper = str(selected_refinery).strip().upper() if selected_refinery else ""
    if sel_ref_upper == "ARTESIA":
        with st.expander("Edit Artesia - RDU Buy Volume"):
            target_ref_name = "ARTESIA RDU"
            
            target_status_val = ""
            if df_mapping is not None and not df_mapping.empty and selected_period and target_ref_name:
                mask_target = (df_mapping['PERIOD'] == selected_period) & (df_mapping['REFINERY'].astype(str).str.strip().str.upper() == target_ref_name)
                val_target = df_mapping[mask_target]['STATUS'].dropna()
                if not val_target.empty:
                    target_status_val = str(val_target.iloc[0]).strip()
            target_status_lower = target_status_val.lower()
            has_target_edit_access = (get_access_level(target_ref_name) == "EDIT")
            
            if "closed" in target_status_lower:
                st.info("Editing is disabled for closed periods.")
            elif not has_target_edit_access:
                st.info("You do not have permission to edit this refinery's data.")
            else:
                if df_raw is not None and not df_raw.empty:
                    refinery_col = "REFINERY_NAME" if "REFINERY_NAME" in df_raw.columns else "REFINERY"
                    if refinery_col in df_raw.columns:
                        df_target = df_raw[df_raw[refinery_col].astype(str).str.strip().str.upper() == target_ref_name].copy()
                        df_artesia_raw = df_raw[df_raw[refinery_col].astype(str).str.strip().str.upper() == "ARTESIA"].copy()
                    else:
                        df_target = pd.DataFrame()
                        df_artesia_raw = pd.DataFrame()
                else:
                    df_target = pd.DataFrame()
                    df_artesia_raw = pd.DataFrame()
                
                artesia_dtb_map = {}
                if not df_artesia_raw.empty:
                    dtb_col = next((c for c in df_artesia_raw.columns if str(c).upper() == 'DAILY_TOTAL_BUY'), None)
                    if dtb_col:
                        for _, r in df_artesia_raw[['DATE_PARSED', dtb_col]].dropna().drop_duplicates(subset=['DATE_PARSED']).iterrows():
                            artesia_dtb_map[r['DATE_PARSED']] = float(r[dtb_col]) if pd.notnull(r[dtb_col]) else 0.0
                    else:
                        grp = df_artesia_raw.groupby('DATE_PARSED')['VOLUME'].sum()
                        for dt_val, vol_val in grp.items():
                            artesia_dtb_map[dt_val] = float(vol_val) if pd.notnull(vol_val) else 0.0

                if not artesia_dtb_map and 'daily_buy_totals' in locals() and daily_buy_totals is not None and not daily_buy_totals.empty:
                    for _, r in daily_buy_totals.iterrows():
                        artesia_dtb_map[r['DATE_PARSED']] = float(r['DAILY_TOTAL_BUY']) if pd.notnull(r['DAILY_TOTAL_BUY']) else 0.0

                if df_target.empty:
                    st.warning("No data available for Artesia - RDU.")
                else:
                    render_edit_buy_volume_artesia_rdu(df_target, artesia_dtb_map, selected_period)

render_edit_buy_volume_section(status_lower, has_edit_access, selected_suppliers, df_filtered, selected_refinery, df_raw, df_mapping, selected_period)
if not selected_suppliers:
    st.info("Please select at least one supplier from the filters above to view the allocation layout.")
else:
    if not df_filtered.empty:
        # Filter for selected suppliers
        df_supp = df_filtered[df_filtered['SUPPLIER_NAME'].isin(selected_suppliers)]
        
        if not df_supp.empty:
            # Find unique groups: Contract Type -> Supplier -> (Pipeline, Premium)
            cols_to_group = [c for c in ['CONTRACT_TYPE_NAME', 'SUPPLIER_NAME', 'PRICING_REGION_NAME'] if c in df_supp.columns]
            if 'DATE_PARSED' in df_supp.columns:
                unique_combos = df_supp.sort_values('DATE_PARSED').drop_duplicates(subset=cols_to_group, keep='last')
            else:
                unique_combos = df_supp.drop_duplicates(subset=cols_to_group, keep='last')
            if 'CONTRACT_TYPE_NAME' in unique_combos.columns and 'SUPPLIER_NAME' in unique_combos.columns:
                unique_combos = unique_combos.sort_values(['CONTRACT_TYPE_NAME', 'SUPPLIER_NAME'])
            
            contracts = {}
            for _, row in unique_combos.iterrows():
                c_type = str(row.get('CONTRACT_TYPE_NAME', 'Unknown'))
                if c_type not in contracts:
                    contracts[c_type] = []
                p_raw = row.get('PREMIUM', None)
                if pd.isna(p_raw) or p_raw is None or str(p_raw).strip().lower() in ('', 'nan', 'none', 'nat'):
                    p_str = '0.0'
                else:
                    p_str = str(p_raw)
                contracts[c_type].append({
                    "supplier": str(row.get('SUPPLIER_NAME', 'Unknown')),
                    "pipeline": str(row.get('PRICING_REGION_NAME', 'N/A')),
                    "premium": p_str
                })
                
            html_lines = []
            html_lines.append("<style>")
            html_lines.append(f".st-volume-wrapper {{ border: 1px solid #d1d2d5; border-radius: 4px; overflow: auto; max-height: 390px; position: relative; background-color: #ffffff; }}")
            html_lines.append(f".st-volume-grid {{ border-collapse: separate; border-spacing: 0; font-family: inherit; font-size: 14px; width: max-content; table-layout: fixed; color: #31333F; background-color: #ffffff; }}")
            html_lines.append("""
            .st-volume-grid th, .st-volume-grid td { padding: 4px 6px; box-sizing: border-box; font-family: inherit; font-size: 14px; color: #31333F; }
            .st-volume-grid th { font-size: 12px !important; }
            .st-volume-grid td { border-bottom: 1px solid #d1d2d5; border-right: 1px solid #d1d2d5; background: #ffffff; }
            .st-volume-grid thead { position: sticky; top: 0; z-index: 50; background-color: #f0f2f6; }
            .st-volume-grid thead th { position: relative; z-index: 50; border-bottom: 1px solid #d1d2d5; border-right: 1px solid #d1d2d5; background: inherit; }
            .st-volume-grid .sticky-col { position: sticky !important; z-index: 30 !important; background: #ffffff !important; left: 0; min-width: 100px; max-width: 100px; box-shadow: 1px 0 0 0 #d1d2d5; }
            .st-volume-grid thead .sticky-col { z-index: 60 !important; background: #f0f2f6 !important; top: 0 !important; }
            .st-volume-grid .sticky-col-daily { position: sticky !important; z-index: 30 !important; background: #ffffff !important; left: 100px; min-width: 130px; max-width: 130px; border-right: none !important; box-shadow: inset -3px 0 0 0 #bebebe; }
            .st-volume-grid thead .sticky-col-daily { z-index: 60 !important; background: #f0f2f6 !important; top: 0 !important; border-right: none !important; box-shadow: inset -3px 0 0 0 #bebebe; }
            .st-volume-grid .thick-border-right { border-right: none !important; box-shadow: inset -3px 0 0 0 #bebebe !important; }
            </style>""")
            html_lines.append("<div class='st-volume-wrapper'>")
            html_lines.append("<table class='st-volume-grid'>")
            html_lines.append("<thead>")
            
            # Row 1: Contract Types
            contract_colors = {}
            c_idx = 0
            for c in contracts.keys():
                contract_colors[c] = "#004438" if c_idx % 2 == 0 else "#cd8a2a"
                c_idx += 1
                
            html_lines.append("<tr class='hdr-r1'>")
            html_lines.append(f"<th rowspan='4' class='sticky-col' style='text-align: center; background-color: #f0f2f6; color: #31333F; font-weight: 400; border-right: 1px solid #d1d2d5 !important; border-bottom: 1px solid #d1d2d5 !important;'>Day</th>")
            html_lines.append(f"<th rowspan='4' class='sticky-col-daily' style='text-align: center; background-color: #f0f2f6; color: #31333F; font-weight: 400; border-right: none !important; border-bottom: 1px solid #d1d2d5 !important;'>Daily Total Buy<br>(MMBtu)</th>")
            for c_type, c_groups in contracts.items():
                colspan = 3 * len(c_groups)
                bg_color = contract_colors[c_type]
                html_lines.append(f"<th colspan='{colspan}' class='thick-border-right' style='background-color: {bg_color}; text-align: center; color: white; font-weight: bold;'>{c_type}</th>")
            html_lines.append("</tr>")
            
            # Row 2: Suppliers
            html_lines.append("<tr class='hdr-r2'>")
            for c_type, c_groups in contracts.items():
                for g in c_groups:
                    html_lines.append(f"<th colspan='3' class='thick-border-right' style='background-color: #013e72; color: white; text-align: center; font-weight: bold;'>{str(g['supplier']).title()}</th>")
            html_lines.append("</tr>")
            
            # Row 3: Premium and Pipeline
            html_lines.append("<tr class='hdr-r3'>")
            for c_type, c_groups in contracts.items():
                for g in c_groups:
                    try:
                        p_float = float(g['premium'])
                        if pd.isna(p_float):
                            p_float = 0.0
                        prem_formatted = f"{p_float:.3f}"
                    except Exception:
                        prem_formatted = "0.000"
                    html_lines.append(f"<th colspan='1' style='background-color: white; color: black; text-align: center;'>Premium<br>{prem_formatted}</th>")
                    html_lines.append(f"<th colspan='2' class='thick-border-right' style='background-color: white; color: black; text-align: center;'>Pricing Region<br>{str(g['pipeline'])}</th>")
            html_lines.append("</tr>")
            
            # Row 4: Volume, Price, Cost (PLAIN STYLE)
            html_lines.append("<tr class='hdr-r4'>")
            for c_type, c_groups in contracts.items():
                for g in c_groups:
                    html_lines.append(f"<th style='background-color: #f0f2f6; color: #31333F; font-weight: 400; text-align: center; width: 120px; min-width: 120px; border-right: 1px solid #d1d2d5 !important; border-bottom: 1px solid #d1d2d5 !important;'>Buy Volume<br>(MMBtu)</th>")
                    html_lines.append(f"<th style='background-color: #f0f2f6; color: #31333F; font-weight: 400; text-align: center; width: 90px; min-width: 90px; border-right: 1px solid #d1d2d5 !important; border-bottom: 1px solid #d1d2d5 !important;'>All in Price</th>")
                    html_lines.append(f"<th class='thick-border-right' style='background-color: #f0f2f6; color: #31333F; font-weight: 400; text-align: center; width: 100px; min-width: 100px; border-bottom: 1px solid #d1d2d5 !important;'>Cost</th>")
            html_lines.append("</tr>")
            html_lines.append("</thead>")
            
            # Data Rows
            html_lines.append("<tbody>")
            unique_days = df_supp[['DATE_PARSED', 'DISPLAY_DAY']].drop_duplicates().sort_values('DATE_PARSED')
            
            for _, day_row in unique_days.iterrows():
                day_str = day_row['DISPLAY_DAY']
                dt = day_row['DATE_PARSED']
                html_lines.append("<tr>")
                html_lines.append(f"<td class='sticky-col' style='background: #ffffff;'>{day_str}</td>")
                
                # Fetch Daily Total Buy from previously computed daily_buy_totals
                daily_buy_val = 0
                if dt in daily_buy_totals['DATE_PARSED'].values:
                    daily_buy_val = daily_buy_totals[daily_buy_totals['DATE_PARSED'] == dt]['DAILY_TOTAL_BUY'].iloc[0]
                
                cell_daily_buy = f"{daily_buy_val:,.2f}"
                html_lines.append(f"<td class='sticky-col-daily' style='text-align: right; background: #ffffff;'>{cell_daily_buy}</td>")
                
                day_data = df_supp[df_supp['DATE_PARSED'] == dt]
                
                for c_type, c_groups in contracts.items():
                    for g in c_groups:
                        match = day_data[
                            (day_data.get('CONTRACT_TYPE_NAME', '').astype(str) == c_type) & 
                            (day_data.get('SUPPLIER_NAME', '').astype(str) == g['supplier']) & 
                            (day_data.get('PRICING_REGION_NAME', '').astype(str) == g['pipeline'])
                        ]
                        
                        vol_val, prc_val, cst_val = "", "", ""
                        if not match.empty:
                            if 'VOLUME' in match.columns and not match['VOLUME'].dropna().empty:
                                tot_v = pd.to_numeric(match['VOLUME'], errors='coerce').sum()
                                if pd.notnull(tot_v):
                                    vol_val = f"{float(tot_v):,.2f}"
                                    
                            if 'ALL_IN_PRICE' in match.columns and not match['ALL_IN_PRICE'].dropna().empty:
                                avg_p = pd.to_numeric(match['ALL_IN_PRICE'], errors='coerce').mean()
                                if pd.notnull(avg_p):
                                    v = float(avg_p)
                                    prc_val = f"$({abs(v):.3f})" if v < 0 else f"${v:.3f}"
                                    
                            if 'COST' in match.columns and not match['COST'].dropna().empty:
                                tot_c = pd.to_numeric(match['COST'], errors='coerce').sum()
                                if pd.notnull(tot_c):
                                    v = float(tot_c)
                                    cst_val = f"$({abs(v):,.2f})" if v < 0 else f"${v:,.2f}"
                                
                        html_lines.append(f"<td style='text-align: right;'>{vol_val}</td>")
                        html_lines.append(f"<td style='text-align: right;'>{prc_val}</td>")
                        html_lines.append(f"<td class='thick-border-right' style='text-align: right;'>{cst_val}</td>")
                html_lines.append("</tr>")
                
            # Totals Row
            html_lines.append("<tr>")
            html_lines.append("<td class='sticky-col' style='font-weight: 600; background: #ffffff;'>Total</td>")
            total_buy_all = daily_buy_totals['DAILY_TOTAL_BUY'].sum()
            cell_total_buy_all = f"{total_buy_all:,.2f}"
            html_lines.append(f"<td class='sticky-col-daily' style='text-align: right; font-weight: 600; background: #ffffff;'>{cell_total_buy_all}</td>")
            
            for c_type, c_groups in contracts.items():
                for g in c_groups:
                    match_all_days = df_supp[
                        (df_supp.get('CONTRACT_TYPE_NAME', '').astype(str) == c_type) & 
                        (df_supp.get('SUPPLIER_NAME', '').astype(str) == g['supplier']) & 
                        (df_supp.get('PRICING_REGION_NAME', '').astype(str) == g['pipeline'])
                    ]
                    
                    t_vol, t_cost = 0, 0
                    if not match_all_days.empty:
                        if 'VOLUME' in match_all_days.columns:
                            t_vol = pd.to_numeric(match_all_days['VOLUME'], errors='coerce').sum()
                        if 'COST' in match_all_days.columns:
                            t_cost = pd.to_numeric(match_all_days['COST'], errors='coerce').sum()
                            
                    vol_str = f"{t_vol:,.2f}"
                    cost_str = f"$({abs(t_cost):,.2f})" if t_cost < 0 else f"${t_cost:,.2f}"
                    
                    html_lines.append(f"<td style='text-align: right; font-weight: 600;'>{vol_str}</td>")
                    html_lines.append(f"<td></td>")
                    html_lines.append(f"<td class='thick-border-right' style='text-align: right; font-weight: 600;'>{cost_str}</td>")
            html_lines.append("</tr>")
            
            html_lines.append("</tbody>")
            html_lines.append("</table>")
            html_lines.append("</div>")
            
            st.markdown("".join(html_lines), unsafe_allow_html=True)
        else:
            st.info("No data available for the selected suppliers.")

# ============================================================
# --- Additional Transactions Section ---
# Allows users to log ad-hoc Buy/Sell transactions not captured in
# the main buy volume flow. Supports adding new rows via a dynamic
# data_editor. Amount is auto-calculated as Volume × Price.
# New rows are inserted into NG_ADDITIONAL_TRANSACTION on Save.
# ============================================================

@st.fragment
def render_additional_transactions_section(selected_period, selected_refinery, status_lower, has_edit_access):
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<h3 style='color: #38A169; font-size: 1.25rem; margin-bottom: 0;'>Additional Transactions</h3>", unsafe_allow_html=True)

    df_add_trans_map = get_additional_trans_mapping(selected_period)

    if not df_add_trans_map.empty and selected_refinery:
        df_add_trans_map = df_add_trans_map[df_add_trans_map['REFINERY_NAME'].str.upper() == selected_refinery.upper()]

    if not df_add_trans_map.empty:
        df_add_trans_map = df_add_trans_map.copy()
        df_add_trans_map['MAPPING_LABEL'] = (
            df_add_trans_map['REFINERY_NAME'].fillna('').astype(str) + ' | ' +
            df_add_trans_map['SUPPLIER_NAME'].fillna('').astype(str) + ' | ' +
            df_add_trans_map['CONTRACT_TYPE_NAME'].fillna('').astype(str) + ' | ' +
            df_add_trans_map['PRICE_REGION_NAME'].fillna('').astype(str) + ' | ' +
            df_add_trans_map['PRICE_CODE_NAME'].fillna('').astype(str)
        )
        mapping_opts = df_add_trans_map['MAPPING_LABEL'].dropna().tolist()
    else:
        mapping_opts = []

    trans_types = ["Buy", "Sell"]

    df_add_trans_cols = [
        "Date", "Transaction Type", "Mapping", "Volume", "Price", "Amount", "REF_SUP_MAPPING_ID"
    ]

    _date_min = None
    _date_max = None
    try:
        _period_dt = pd.to_datetime(selected_period)
        _date_min = _period_dt.date().replace(day=1)
        if _period_dt.month == 12:
            _date_max = _period_dt.date().replace(day=31)
        else:
            _date_max = (_period_dt.replace(month=_period_dt.month + 1, day=1) - pd.Timedelta(days=1)).date()
    except Exception:
        pass

    current_context = f"{selected_period}_{selected_refinery}"
    if st.session_state.get("add_trans_context") != current_context:
        st.session_state.add_trans_context = current_context
        if _date_min and _date_max and selected_refinery:
            fetched_df = fetch_additional_transactions(selected_refinery, _date_min, _date_max, selected_period)
            if not fetched_df.empty:
                st.session_state.add_trans_df_state = fetched_df
                st.session_state.add_trans_df_original = fetched_df.copy()
            else:
                st.session_state.add_trans_df_state = pd.DataFrame(columns=df_add_trans_cols)
                st.session_state.add_trans_df_original = pd.DataFrame(columns=df_add_trans_cols)
        else:
            st.session_state.add_trans_df_state = pd.DataFrame(columns=df_add_trans_cols)
            st.session_state.add_trans_df_original = pd.DataFrame(columns=df_add_trans_cols)
    elif "add_trans_df_state" not in st.session_state:
        st.session_state.add_trans_df_state = pd.DataFrame(columns=df_add_trans_cols)
        st.session_state.add_trans_df_original = pd.DataFrame(columns=df_add_trans_cols)

    df_for_editor = st.session_state.add_trans_df_state.reset_index(drop=True).copy()
    df_original = st.session_state.get("add_trans_df_original", pd.DataFrame()).copy()

    if "Delete" not in df_for_editor.columns:
        df_for_editor.insert(0, "Delete", False)

    if "Date" in df_for_editor.columns and not df_for_editor.empty:
        df_for_editor["Date"] = pd.to_datetime(df_for_editor["Date"], errors="coerce")
        df_for_editor = df_for_editor.sort_values(by="Date", ascending=True, na_position="last").reset_index(drop=True)
    if "Date" in df_original.columns and not df_original.empty:
        df_original["Date"] = pd.to_datetime(df_original["Date"], errors="coerce")
        df_original = df_original.sort_values(by="Date", ascending=True, na_position="last").reset_index(drop=True)

    if "Amount" in df_for_editor.columns and not df_for_editor.empty:
        df_for_editor["Amount"] = pd.to_numeric(df_for_editor["Amount"], errors="coerce")

    is_closed = "closed" in status_lower
    is_disabled = is_closed or not has_edit_access

    column_config_add_trans = {
        "Delete": st.column_config.CheckboxColumn("Delete", default=False, disabled=is_disabled),
        "Date": st.column_config.DateColumn(
            "Date",
            format="MM/DD/YYYY",
            required=True,
            min_value=_date_min,
            max_value=_date_max,
        ),
        "Transaction Type": st.column_config.SelectboxColumn("Transaction Type", options=trans_types, required=True),
        "Mapping": st.column_config.SelectboxColumn(
            "Refinery | Supplier | Contract Type | Price Region | Price Code",
            options=mapping_opts,
            required=True,
            width="large"
        ),
        "Volume": st.column_config.TextColumn("Volume", required=True),
        "Price": st.column_config.TextColumn("Price", required=True),
        "Amount": st.column_config.NumberColumn("Amount", disabled=True, format="%,.2f"),
        "REF_SUP_MAPPING_ID": None,
    }

    if "editor_key_counter" not in st.session_state:
        st.session_state.editor_key_counter = 0

    dynamic_key = f"add_trans_editor_{st.session_state.editor_key_counter}"

    if is_closed:
        st.info("Editing is disabled for closed periods.")
    elif not has_edit_access:
        st.info("You do not have permission to edit this refinery's data.")

    edited_df_add_trans = st.data_editor(
        df_for_editor,
        column_config=column_config_add_trans,
        hide_index=True,
        num_rows="dynamic" if not is_disabled else "fixed",
        use_container_width=True,
        disabled=is_disabled,
        key=dynamic_key
    )

    def recalc_amount(df):
        df = df.copy()
        for idx in df.index:
            try:
                v_raw = df.at[idx, "Volume"]
                p_raw = df.at[idx, "Price"]
                if pd.notna(v_raw) and str(v_raw).strip() != "" and pd.notna(p_raw) and str(p_raw).strip() != "":
                    v = float(str(v_raw).replace(",", ""))
                    p = float(str(p_raw).replace(",", ""))
                    if v != 0 and p != 0:
                        df.at[idx, "Amount"] = round(v * p, 2)
                    else:
                        df.at[idx, "Amount"] = None
                else:
                    df.at[idx, "Amount"] = None
            except Exception:
                df.at[idx, "Amount"] = None
        return df

    edited_df_add_trans_new = recalc_amount(edited_df_add_trans).reset_index(drop=True)

    # --- Delete Handler & Button UI matching contacts.py ---
    editor_state = st.session_state.get(dynamic_key, {})
    edited_rows = editor_state.get("edited_rows", {})

    has_add_trans_deletes = any(edits.get("Delete") is True for edits in edited_rows.values())
    if not has_add_trans_deletes and "Delete" in edited_df_add_trans_new.columns:
        has_add_trans_deletes = bool(edited_df_add_trans_new["Delete"].any())

    if st.button("Delete Selected Records", key="btn_delete_add_trans", type="secondary", disabled=not has_add_trans_deletes or is_disabled):
        delete_indices = set()
        for str_idx, edits in edited_rows.items():
            if edits.get("Delete") is True:
                try:
                    delete_indices.add(int(str_idx))
                except Exception:
                    pass
        if "Delete" in edited_df_add_trans_new.columns:
            for idx, is_del in enumerate(edited_df_add_trans_new["Delete"]):
                if is_del is True or is_del == True:
                    delete_indices.add(idx)

        try:
            _p_dt = pd.to_datetime(selected_period, errors='coerce')
            first_day_of_month_str = _p_dt.replace(day=1).strftime('%Y-%m-%d') if _p_dt is not None and not pd.isna(_p_dt) else pd.Timestamp.now().replace(day=1).strftime('%Y-%m-%d')
        except Exception:
            first_day_of_month_str = pd.Timestamp.now().replace(day=1).strftime('%Y-%m-%d')

        deletes_to_process = sorted(list(delete_indices))
        if deletes_to_process:
            try:
                conn = st.connection("snowflake")
                session = conn.session()
                
                try:
                    if hasattr(st, "user") and hasattr(st.user, "user_name") and st.user.user_name:
                        username = st.user.user_name
                    else:
                        username = "streamlit user"
                except Exception:
                    username = "streamlit user"

                deleted_count = 0
                sql_statements = []
                for row_idx in deletes_to_process:
                    if row_idx < len(edited_df_add_trans_new):
                        row = edited_df_add_trans_new.iloc[row_idx]
                        c_date = row.get("Date")
                        c_map = str(row.get("Mapping", "")).strip()
                        c_type = str(row.get("Transaction Type", "Buy")).strip()
                        c_vol = row.get("Volume")
                        c_price = row.get("Price")
                        
                        if pd.notna(c_date) and c_map:
                            cal_date_str = pd.to_datetime(c_date).strftime('%Y-%m-%d')
                            
                            ref_sup_mapping_id = row.get("REF_SUP_MAPPING_ID")
                            if pd.isna(ref_sup_mapping_id) or not ref_sup_mapping_id:
                                match = df_add_trans_map[df_add_trans_map['MAPPING_LABEL'] == c_map]
                                if not match.empty:
                                    ref_sup_mapping_id = match['REF_SUP_MAPPING_ID'].iloc[0]

                            if ref_sup_mapping_id:
                                vol_str = str(c_vol).replace(",", "").strip() if pd.notna(c_vol) else ""
                                price_str = str(c_price).replace(",", "").strip() if pd.notna(c_price) else ""
                                
                                vol_clause = f"AND TRANSACTION_VOLUME = {int(float(vol_str))}" if vol_str else ""
                                price_clause = f"AND TRANSACTION_PRICE = {float(price_str)}" if price_str else ""

                                expire_sql = f"""
                                    UPDATE CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_ADDITIONAL_TRANSACTION
                                    SET RECORD_END_TS = '{first_day_of_month_str}', UPDATED_BY = '{username}'
                                    WHERE REF_SUP_MAPPING_ID = '{ref_sup_mapping_id}'
                                      AND CALENDAR_DATE = '{cal_date_str}'
                                      AND TRANSACTION_TYPE = '{c_type}'
                                      {vol_clause}
                                      {price_clause}
                                      AND RECORD_END_TS IS NULL
                                """
                                sql_statements.append(expire_sql)
                                deleted_count += 1

                if sql_statements:
                    sp_call = get_sp_burn_tracker_volume_call(selected_period, selected_refinery)
                    if sp_call:
                        sql_statements.append(sp_call)

                    execute_batch_sql(session, sql_statements)

                fetch_additional_transactions.clear()
                st.cache_data.clear()
                st.session_state.show_add_trans_delete_msg = f"Successfully deleted {deleted_count} record(s)!" if deleted_count > 0 else "Selected record(s) removed."
                st.session_state.editor_key_counter += 1
                st.session_state.add_trans_context = None
                st.rerun()
            except Exception as e:
                st.error(f"Error deleting records: {e}")

    st.markdown("<br>", unsafe_allow_html=True)
    if "confirm_changes_key_counter_add_trans" not in st.session_state:
        st.session_state.confirm_changes_key_counter_add_trans = 0
    chk_key = f"confirm_changes_add_trans_{st.session_state.confirm_changes_key_counter_add_trans}"

    chk_col, save_col, msg_col = st.columns([2.2, 1.3, 6.4])
    with chk_col:
        confirm_changes = st.checkbox("Confirm Changes", key=chk_key, disabled=is_disabled)

    with msg_col:
        if st.session_state.get("show_add_trans_delete_msg"):
            st.toast(st.session_state.show_add_trans_delete_msg)
            del st.session_state["show_add_trans_delete_msg"]

        if st.session_state.get("show_add_trans_success", False):
            st.toast("Successfully saved Additional Transactions!")
            st.session_state.show_add_trans_success = False
            
        if st.session_state.get("show_add_trans_info", False):
            st.toast("No valid complete rows to save. Please fill all required fields and ensure the mapping exists.")
            st.session_state.show_add_trans_info = False

    with save_col:
        if st.button("Save", type="primary", disabled=not confirm_changes, key="save_add_trans_btn"):
            changes_made = False
            try:
                conn = st.connection("snowflake")
                session = conn.session()

                try:
                    _p_dt = pd.to_datetime(selected_period, errors='coerce')
                    first_day_of_month_str = _p_dt.replace(day=1).strftime('%Y-%m-%d') if _p_dt is not None and not pd.isna(_p_dt) else pd.Timestamp.now().replace(day=1).strftime('%Y-%m-%d')
                except Exception:
                    first_day_of_month_str = pd.Timestamp.now().replace(day=1).strftime('%Y-%m-%d')
                
                try:
                    if hasattr(st, "user") and hasattr(st.user, "user_name") and st.user.user_name:
                        username = st.user.user_name
                    else:
                        username = "streamlit user"
                except Exception:
                    username = "streamlit user"
                
                valid_rows = edited_df_add_trans_new.dropna(subset=["Date", "Mapping", "Volume", "Price"])
                if "Delete" in valid_rows.columns:
                    valid_rows = valid_rows[valid_rows["Delete"] != True]
                valid_rows = valid_rows[valid_rows["Mapping"].astype(str).str.strip() != ""]
                
                def make_row_key(r):
                    c_d = pd.to_datetime(r.get('Date')).strftime('%Y-%m-%d') if pd.notna(r.get('Date')) else ''
                    c_m = str(r.get('Mapping', '')).strip()
                    c_t = str(r.get('Transaction Type', 'Buy')).strip().capitalize()
                    c_v = str(r.get('Volume', '')).replace(',', '').strip()
                    c_p = str(r.get('Price', '')).replace(',', '').strip()
                    try:
                        v_num = f"{int(float(c_v))}" if c_v else ""
                    except Exception:
                        v_num = c_v
                    try:
                        p_num = f"{float(c_p):.2f}" if c_p else ""
                    except Exception:
                        p_num = c_p
                    return (c_d, c_m, c_t, v_num, p_num)

                original_key_map = {}
                for _, r in df_original.iterrows():
                    k = make_row_key(r)
                    if k[0] and k[1]:
                        original_key_map[k] = r

                valid_key_map = {}
                for _, r in valid_rows.iterrows():
                    k = make_row_key(r)
                    if k[0] and k[1]:
                        valid_key_map[k] = r

                updates_sql_list = []
                inserts_val_list = []

                # 1. Expire edited / removed original rows (RECORD_END_TS = first_day_of_month_str)
                for orig_k, orig_row in original_key_map.items():
                    if orig_k not in valid_key_map:
                        orig_date_str = orig_k[0]
                        orig_map = orig_k[1]
                        orig_type = orig_k[2]
                        orig_vol_str = orig_k[3]
                        orig_price_str = orig_k[4]

                        orig_ref_sup_id = orig_row.get("REF_SUP_MAPPING_ID")
                        if pd.isna(orig_ref_sup_id) or not orig_ref_sup_id:
                            m_match = df_add_trans_map[df_add_trans_map['MAPPING_LABEL'] == orig_map]
                            if not m_match.empty:
                                orig_ref_sup_id = m_match['REF_SUP_MAPPING_ID'].iloc[0]

                        if orig_ref_sup_id:
                            vol_c = f"AND TRANSACTION_VOLUME = {orig_vol_str}" if orig_vol_str else ""
                            price_c = f"AND TRANSACTION_PRICE = {orig_price_str}" if orig_price_str else ""
                            expire_sql = f"""
                                UPDATE CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_ADDITIONAL_TRANSACTION
                                SET RECORD_END_TS = '{first_day_of_month_str}', UPDATED_BY = '{username}'
                                WHERE REF_SUP_MAPPING_ID = '{orig_ref_sup_id}'
                                  AND CALENDAR_DATE = '{orig_date_str}'
                                  AND TRANSACTION_TYPE = '{orig_type}'
                                  {vol_c}
                                  {price_c}
                                  AND RECORD_END_TS IS NULL
                            """
                            updates_sql_list.append(expire_sql)
                            changes_made = True

                # 2. Insert new / edited rows (RECORD_START_TS = first_day_of_month_str, RECORD_END_TS = NULL)
                for new_k, new_row in valid_key_map.items():
                    if new_k not in original_key_map:
                        cal_date_str = new_k[0]
                        c_map = new_k[1]
                        trans_type = new_k[2]
                        vol = int(float(new_k[3])) if new_k[3] else 0
                        price = float(new_k[4]) if new_k[4] else 0.0

                        match = df_add_trans_map[df_add_trans_map['MAPPING_LABEL'] == c_map]
                        if not match.empty:
                            ref_sup_mapping_id = match['REF_SUP_MAPPING_ID'].iloc[0]
                            inserts_val_list.append(f"('{cal_date_str}', '{ref_sup_mapping_id}', '{trans_type}', {vol}, {price}, '{first_day_of_month_str}', NULL, '{username}')")
                            changes_made = True
                
                if updates_sql_list or inserts_val_list:
                    all_statements = list(updates_sql_list)
                    if inserts_val_list:
                        bulk_insert_sql = f"""
                            INSERT INTO CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_ADDITIONAL_TRANSACTION
                            (CALENDAR_DATE, REF_SUP_MAPPING_ID, TRANSACTION_TYPE, TRANSACTION_VOLUME, TRANSACTION_PRICE, RECORD_START_TS, RECORD_END_TS, UPDATED_BY)
                            VALUES
                            {', '.join(inserts_val_list)}
                        """
                        all_statements.append(bulk_insert_sql)
                    
                    sp_call = get_sp_burn_tracker_volume_call(selected_period, selected_refinery)
                    if sp_call:
                        all_statements.append(sp_call)

                    execute_batch_sql(session, all_statements)
                
                if changes_made:
                    st.session_state.show_add_trans_success = True
                    st.session_state.confirm_changes_key_counter_add_trans += 1
                    st.session_state.add_trans_context = None
                    fetch_additional_transactions.clear()
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.session_state.show_add_trans_info = True
                    st.session_state.confirm_changes_key_counter_add_trans += 1
                    st.rerun()
                    
            except Exception as e:
                st.error(f"Error saving to Snowflake: {e}")

render_additional_transactions_section(selected_period, selected_refinery, status_lower, has_edit_access)

# --- Render Financial Review Metrics into Top Container ---
# Runs after main tables are displayed so Daily Readings, Buy Volume & Additional Transactions render FIRST.
@st.fragment
def render_frm_expander_section(selected_period, selected_refinery, df_filtered, df_readings_filtered, df_fuel_filtered):
    with st.expander("Financial Review Metrics"):
        if selected_period and selected_refinery:
            with st.spinner("Loading Financial Review Metrics..."):
                df_pricing = get_frm_pricing(selected_period, selected_refinery)
                df_contract = get_frm_price_contract(selected_period, selected_refinery)
                df_misc = get_frm_misc(selected_period, selected_refinery)

                total_burn_val = 0.0
                if not df_readings_filtered.empty:
                    disp_burns = get_display_burn_totals(df_readings_filtered, selected_refinery)
                    total_burn_val = float(sum(disp_burns)) if disp_burns else 0.0

                grid_imb, grid_buy = compute_grid_imbalance_and_buy_volume(
                    selected_period, selected_refinery, df_filtered, df_readings_filtered, df_fuel_filtered
                )

                render_frm_section(df_pricing, df_contract, df_misc, total_burn=total_burn_val, grid_imbalance=grid_imb, grid_total_buy=grid_buy)
        else:
            st.info("Please select a period and refinery to view Financial Review Metrics.")

with frm_container:
    render_frm_expander_section(selected_period, selected_refinery, df_filtered, df_readings_filtered, df_fuel_filtered)