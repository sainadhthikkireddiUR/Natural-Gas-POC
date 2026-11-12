import streamlit as st
import pandas as pd

from common import render_header, derive_status, render_status_pill, is_locked, get_access_level, format_accounting, parse_accounting, seed_shared_filter, save_shared_filter

#st.set_page_config(page_title="Pricing", layout="wide", initial_sidebar_state="expanded")

render_header()


def get_filter_mapping():
    conn = st.connection("snowflake")
    try:
        df_mapping = conn.query("SELECT DISTINCT PERIOD, REFINERY, STATUS FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.ng_dashboard_filter_vw", ttl=0)
        return df_mapping
    except Exception:
        return pd.DataFrame(columns=["PERIOD", "REFINERY", "STATUS"])

df_mapping = get_filter_mapping()

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

        if isinstance(refinery_name, (int, float)) and pd.notna(refinery_name):
            ref_id_str = str(int(refinery_name))
        elif isinstance(refinery_name, str) and refinery_name.isdigit():
            ref_id_str = refinery_name
        else:
            ref_id = get_refinery_id_by_name(refinery_name)
            ref_id_str = str(ref_id) if ref_id is not None else "NULL"

        return f"CALL CONSUMPTION.HFS_COMMERCIAL_ONLY.SP_BURN_TRACKER_VOLUME({ref_id_str}, {month}, {year})"
    except Exception:
        return None


# Get Period list (sorted by date)
if not df_mapping.empty and 'PERIOD' in df_mapping.columns:
    periods_unique = df_mapping['PERIOD'].dropna().unique()
    temp_df = pd.DataFrame({'PERIOD': periods_unique})
    temp_df['temp'] = pd.to_datetime(temp_df['PERIOD'], errors='coerce')
    periods_list = temp_df.dropna(subset=['temp']).sort_values(by='temp', ascending=False)['PERIOD'].astype(str).tolist()
    unparseable = [p for p in periods_unique if str(p) not in periods_list]
    periods_list.extend(unparseable)
else:
    periods_list = []

# Top Filters
col1, col2, col_blank, col_spacer, col_save = st.columns([1.5, 1.5, 1.2, 0.6, 1.2])
with col1:
    seed_shared_filter("period", "pricing_month", periods_list)
    month_year = st.selectbox("Period", periods_list, key="pricing_month")
    save_shared_filter("period", month_year)

with col2:
    if not df_mapping.empty and month_year:
        refineries_list = sorted(df_mapping[df_mapping['PERIOD'] == month_year]['REFINERY'].dropna().astype(str).unique().tolist())
    else:
        refineries_list = []
    
    seed_shared_filter("refinery", "pricing_refinery", refineries_list)
    refinery = st.selectbox("Refinery", refineries_list, key="pricing_refinery")
    save_shared_filter("refinery", refinery)

status_val = derive_status(df_mapping, month_year, refinery)
period_locked = is_locked(status_val)
access_level = get_access_level(refinery)
view_only = access_level != "EDIT"
read_only = period_locked or view_only

with col_blank:
    render_status_pill(status_val)
    st.markdown("""
    <style>
    /* Set font size for confirm changes label to 1rem */
    div[data-testid="stCheckbox"] p {
        font-size: 1rem !important;
    }
    /* Align checkbox container and pull the save button up to align with inputs */
    div.element-container:has(div[data-testid="stCheckbox"]) {
        width: 85% !important;
        margin-left: auto !important;
        margin-bottom: -16px !important;
    }
    /* Reduce the size of the save button and align it */
    div[data-testid="stButton"] button[kind="primary"] {
        width: 85% !important;
        margin-left: auto !important;
        display: block !important;
    }
    </style>
    """, unsafe_allow_html=True)

check_ph = col_save.empty()
save_ph = col_save.empty()


@st.cache_data(show_spinner=False)
def get_pricing_master_data(selected_refinery):
    conn = st.connection("snowflake")
    query = f"""
    SELECT pm.*
    FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_PRICING_MASTER_VW pm
    WHERE pm.PRICE_QUOTE_ID IN (
        SELECT DISTINCT PRICE_CODE_ID 
        FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_PRICE_CODE_MAPPING_VW
        WHERE REFINERY = '{selected_refinery}'
    )
    """
    df = conn.query(query)
    df.columns = [col.upper() for col in df.columns]
    return df

df_all = get_pricing_master_data(refinery)

if month_year:
    try:
        my_dt = pd.to_datetime(month_year, errors='coerce')
        if pd.notna(my_dt):
            df_dates = pd.to_datetime(df_all['PRICE_DATE'], errors='coerce')
            df_all = df_all[((df_dates.dt.year == my_dt.year) & (df_dates.dt.month == my_dt.month)) | df_all['PRICE_DATE'].isnull()]
    except Exception:
        pass

def create_pivoted_table(df_subset, add_avg_row=False):
    if df_subset.empty:
        return pd.DataFrame(columns=["Date"])
        
    df_symbols = df_subset[['SYMBOL', 'DESCRIPTION']].drop_duplicates().sort_values(by='SYMBOL')
    symbols = df_symbols['SYMBOL'].dropna().tolist()
    descriptions = df_symbols['DESCRIPTION'].dropna().tolist()
    
    pivot_df = df_subset.pivot_table(index='PRICE_DATE', columns='SYMBOL', values='PRICE', aggfunc='first', dropna=False).reset_index()
    
    def format_date(d):
        if pd.isnull(d) or str(d).strip() == "" or str(d).strip().lower() in ["nat", "nan"]:
            return ""
        try:
            dt = pd.to_datetime(d)
            # Pad with non-breaking spaces to visually center the text in the data editor canvas
            return dt.strftime('%d-%b-%Y')
        except:
            return str(d)
            
    pivot_df['PRICE_DATE'] = pivot_df['PRICE_DATE'].apply(format_date)
    pivot_df.rename(columns={'PRICE_DATE': 'Date'}, inplace=True)
    
    for sym in symbols:
        if sym not in pivot_df.columns:
            pivot_df[sym] = None
            
    cols = ['Date'] + symbols
    pivot_df = pivot_df[cols]
    
    empty_codes = pivot_df[pivot_df['Date'] == '']
    non_empty_codes = pivot_df[pivot_df['Date'] != '']
    try:
        # Strip the non-breaking spaces before sorting
        non_empty_codes = non_empty_codes.sort_values(by='Date', key=lambda col: pd.to_datetime(col.str.strip(), format='%d-%b-%Y'))
    except Exception:
        pass
        
    pivot_df = pd.concat([empty_codes, non_empty_codes], ignore_index=True)
    
    # Rename columns to "Symbol - Description"
    rename_mapping = {sym: f"{sym} - {desc}" if pd.notna(desc) and str(desc).strip() != "" else sym for sym, desc in zip(symbols, descriptions)}
    pivot_df.rename(columns=rename_mapping, inplace=True)
    new_symbols = [rename_mapping[sym] for sym in symbols]
    new_cols = ['Date'] + new_symbols
    
    if add_avg_row:
        avg_vals = pivot_df[new_symbols].apply(pd.to_numeric, errors='coerce').mean()
        avg_row_data = ['Avg Price'] + [round(avg_vals.get(nsym, 0), 4) if pd.notnull(avg_vals.get(nsym)) else '' for nsym in new_symbols]
        avg_row = pd.DataFrame([avg_row_data], columns=new_cols)
        final_df = pd.concat([pivot_df, avg_row], ignore_index=True)
    else:
        final_df = pivot_df.copy()
        
    return final_df


if "show_pricing_toast" in st.session_state:
    st.toast(st.session_state.show_pricing_toast)
    del st.session_state["show_pricing_toast"]

if st.session_state.get("reset_confirm_pricing", False):
    if "confirm_fom" in st.session_state:
        st.session_state.confirm_fom = False
    if "confirm_gas" in st.session_state:
        st.session_state.confirm_gas = False
    st.session_state.reset_confirm_pricing = False


# ============================================================================
# NEW: popup dialog shown when a Premium/Start Date/End Date edit (or a new
# "Add Period" submission) would overlap another active period for that mapping
# ============================================================================
@st.dialog("Overlap Detected")
def show_overlap_dialog(messages):
    st.write("The following change(s) could not be saved because they overlap with an existing active period:")
    for msg in messages:
        st.markdown(f"- {msg}")
    st.write("Please adjust the Start/End Date so periods for the same mapping do not overlap, then try again.")
    if st.button("OK", key="btn_close_overlap_dialog", type="primary"):
        st.rerun()


# ============================================================================
# ORIGINAL get_refinery_mapping_data() — commented out.
# Premium/Start Date/End Date now live in CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
# instead of being read out of NG_REFINERY_PRICE_CODE_MAPPING_VW, so this is
# replaced by the version below that reads the new NG_REF_SUPPLIER_PREMIUM_VW view.
# ============================================================================
def get_refinery_mapping_data():
    conn = st.connection("snowflake")
    query = "select distinct refinery, period, supplier, contract_type, pricing_region, price_code, price_code_desc, start_date, end_date, premium, ref_sup_mapping_id, refinery_id, supplier_id, contract_type_id, pricing_region_id, price_code_id from CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_PRICE_CODE_MAPPING_VW order by refinery, supplier, contract_type, pricing_region, price_code, start_date"
    df = conn.query(query)
    df.columns = [str(col).replace('_', ' ').title() for col in df.columns]
    df = df.rename(columns={"Price Code Desc": "Price Code Description"})
    return df

# def get_refinery_mapping_data():
#     conn = st.connection("snowflake")
#     query = """
#         SELECT DISTINCT
#             REFINERY_NAME, CALENDAR_YEAR, MONTH_NUMBER, SUPPLIER_NAME, CONTRACT_TYPE_NAME,
#             PRICING_REGION_NAME, PRICE_CODE_ID, PRICE_CODE_NAME, EFFECTIVE_START_DT, EFFECTIVE_END_DT, PREMIUM,
#             REF_SUP_MAPPING_ID, REFINERY_ID, SUPPLIER_ID, CONTRACT_TYPE_ID, PRICING_REGION_ID
#         FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REF_SUPPLIER_PREMIUM_VW
#         ORDER BY REFINERY_NAME, SUPPLIER_NAME, CONTRACT_TYPE_NAME, PRICING_REGION_NAME, PRICE_CODE_NAME, EFFECTIVE_START_DT
#     """
#     df = conn.query(query, ttl=0)
#     df.columns = [str(col).replace('_', ' ').title() for col in df.columns]
#     # rename to match the column names the rest of this page already expects
#     df = df.rename(columns={
#         "Refinery Name": "Refinery",
#         "Supplier Name": "Supplier",
#         "Contract Type Name": "Contract Type",
#         "Pricing Region Name": "Pricing Region",
#         "Price Code Name": "Price Code",
#         "Effective Start Dt": "Start Date",
#         "Effective End Dt": "End Date",
#     })
#     return df

df_mapping = get_refinery_mapping_data()
if refinery and 'Refinery' in df_mapping.columns:
    df_mapping = df_mapping[df_mapping["Refinery"] == refinery]

# ============================================================================
# ORIGINAL period filter — commented out. The new view exposes Calendar Year /
# Month Number instead of a single Period column, so filtering changes below.
# ============================================================================
if month_year and 'Period' in df_mapping.columns:
    temp_my = pd.to_datetime(df_mapping['Period'], errors='coerce').dt.strftime('%b-%y')
    df_mapping = df_mapping[(temp_my == month_year) | (df_mapping['Period'] == month_year) | df_mapping['Period'].isnull()]

# if month_year and 'Calendar Year' in df_mapping.columns and 'Month Number' in df_mapping.columns:
#     try:
#         _sel_dt = pd.to_datetime(month_year, errors='coerce')
#         if pd.notna(_sel_dt):
#             df_mapping = df_mapping[
#                 (df_mapping['Calendar Year'] == _sel_dt.year) & (df_mapping['Month Number'] == _sel_dt.month)
#             ]
#     except Exception:
#         pass

# df_mapping = df_mapping.reset_index(drop=True)

# ---- Drop "ghost" rows created by the view's per-day LEFT JOIN ----
# If a mapping has real premium data for part of the selected month, the view
# also emits a separate all-NULL row representing the uncovered remainder of
# that month (a genuine gap, which we're fine with — but it shouldn't display
# as a blank duplicate row). Only keep the NULL row when a mapping has NO real
# premium data at all in this result set (so brand-new mappings still show up
# as a single blank row rather than disappearing entirely).
if not df_mapping.empty and "Ref Sup Mapping Id" in df_mapping.columns:
    _has_real_data = df_mapping.groupby("Ref Sup Mapping Id")["Start Date"].transform(lambda s: s.notna().any())
    df_mapping = df_mapping[df_mapping["Start Date"].notna() | (~_has_real_data)].reset_index(drop=True)

# --- FoM Table ---
df_fom_db = df_all[df_all['CONTRACT_TYPE'].astype(str).str.upper().str.contains('FOM|FIRST OF MONTH', na=False, regex=True)]
df_fom_raw = create_pivoted_table(df_fom_db)

if not df_fom_db.empty and len(df_fom_raw.columns) > 1:
    symbols = [c for c in df_fom_raw.columns if c != 'Date']
    
    fom_row = {"Price Code - Description": "FOM"}
    rider_row = {"Price Code - Description": "Pricing Rider"}
    all_in_row = {"Price Code - Description": "All in FOM"}
    
    for sym in symbols:
        val = df_fom_raw[sym].iloc[0] if not df_fom_raw.empty else float('nan')
        fom_v = float(val) if pd.notna(val) and val != '' else float('nan')
        fom_row[sym] = fom_v
        
        pc = sym.split(' - ')[0].strip()
        mapping_subset = df_mapping[
            (df_mapping['Price Code'].astype(str) == pc) &
            (df_mapping['Contract Type'].astype(str).str.upper().str.contains('FOM|FIRST OF MONTH', na=False, regex=True))
        ].copy()
        rider_val = float('nan')
        if not mapping_subset.empty:
            mapping_subset['Premium'] = pd.to_numeric(mapping_subset['Premium'], errors='coerce')
            m_val = mapping_subset['Premium'].mean()
            if pd.notna(m_val):
                rider_val = float(m_val)
        rider_row[sym] = rider_val
        
        if pd.notna(fom_v) and pd.notna(rider_val):
            all_in_row[sym] = fom_v + rider_val
        elif pd.notna(fom_v):
            all_in_row[sym] = fom_v
        elif pd.notna(rider_val):
            all_in_row[sym] = rider_val
        else:
            all_in_row[sym] = float('nan')
            
    df_fom = pd.DataFrame([fom_row, rider_row, all_in_row])
else:
    df_fom = pd.DataFrame(columns=["Price Code - Description"])

st.markdown("<h3 style='color: #38A169; font-size: 1.25rem;'>FOM Pricing</h3>", unsafe_allow_html=True)

if df_fom.empty or len(df_fom.columns) <= 1:
    st.info("No data available for the selected filters.")
else:
    df_fom_display = df_fom.copy()
    fom_col_cfg = {"Price Code - Description": st.column_config.Column("Price Code - Description", width=160, disabled=True)}
    for col in df_fom_display.columns:
        if col != "Price Code - Description":
            df_fom_display[col] = df_fom_display[col].map(lambda x: format_accounting(x, 4, is_currency=True))
            fom_col_cfg[col] = st.column_config.NumberColumn(f"{col} ($/MMBtu)", width=262, disabled=True)
    
    # styled_fom = df_fom_display.style.set_properties(
    #     subset=[col for col in df_fom_display.columns if col != "Price Code - Description"],
    #     **{'text-align': 'right'}
    # )
    styled_fom = df_fom_display.style.set_properties(
        subset=[col for col in df_fom_display.columns if col != "Price Code - Description"]
    )
    
    st.data_editor(styled_fom, use_container_width=False, hide_index=True, disabled=True, column_config=fom_col_cfg, key="fom_editor")

# --- Gas Daily Pricing Table ---
df_daily_db = df_all[df_all['CONTRACT_TYPE'].astype(str).str.upper().str.contains('GAS DAILY', na=False, regex=True)]
df_gas_daily = create_pivoted_table(df_daily_db, add_avg_row=True)

st.markdown("<h3 style='color: #38A169; font-size: 1.25rem;'>Gas Daily Pricing</h3>", unsafe_allow_html=True)

if len(df_gas_daily.columns) <= 1:
    st.info("No data available for the selected filters.")
else:
    def highlight_avg_row(row):
        if row['Date'] == 'Avg Price':
            return ['font-weight: bold; color: #333;'] * len(row)
        return [''] * len(row)

    df_gas_daily_display = df_gas_daily.copy()
    gas_col_cfg = {"Date": st.column_config.Column("Calender Date", width=120, disabled=True)}
    for col in df_gas_daily_display.columns:
        if col != "Date":
            df_gas_daily_display[col] = df_gas_daily_display[col].map(lambda x: format_accounting(x, 4, is_currency=True))
            gas_col_cfg[col] = st.column_config.NumberColumn(f"{col} ($/MMBtu)", width=315, disabled=True)

    styled_gas_daily = df_gas_daily_display.style.apply(highlight_avg_row, axis=1).set_properties(
        subset=[col for col in df_gas_daily_display.columns if col != "Date"]
        
    )
    st.data_editor(styled_gas_daily, use_container_width=False, hide_index=True, disabled=True, column_config=gas_col_cfg, key="gas_daily_editor")


# ============================================================================
# NEW helper: username resolution kept identical to the original inline logic
# used inside save_refinery_mapping(), just pulled out so both the grid-save
# and the new Add-Period form can share it.
# ============================================================================
def _get_username():
    user_name_val = None
    try:
        if hasattr(st, "user") and hasattr(st.user, "user_name") and st.user.user_name:
            user_name_val = st.user.user_name
        elif hasattr(st, "user") and hasattr(st.user, "email") and st.user.email:
            user_name_val = st.user.email
    except Exception:
        pass
    if not user_name_val:
        user_name_val = st.session_state.get("current_username", "streamlit user")
    return user_name_val


# ============================================================================
# NEW helper: overlap check against NG_REFINERY_SUP_PREMIUM for a given mapping.
# ============================================================================
def check_overlap(session, mapping_id, new_start, new_end, exclude_start_dt=None):
    new_end_expr = f"'{new_end}'" if new_end else "'9999-12-31'"
    exclude_clause = f"AND EFFECTIVE_START_DT != '{exclude_start_dt}'" if exclude_start_dt else ""
    result = session.sql(f"""
        SELECT COUNT(*) AS CNT
        FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
        WHERE REF_SUP_MAPPING_ID = {mapping_id}
          AND RECORD_END_TS IS NULL
          {exclude_clause}
          AND '{new_start}' <= COALESCE(EFFECTIVE_END_DT, '9999-12-31')
          AND {new_end_expr} >= EFFECTIVE_START_DT
    """).collect()
    return result[0]["CNT"] > 0


# ============================================================================
# NEW save_refinery_mapping(): writes to NG_REFINERY_SUP_PREMIUM instead of
# NG_REFINERY_SUPPLIER_MAPPING. Detects Premium / Start Date / End Date changes,
# validates Start Date is present and End Date >= Start Date, runs an overlap
# check before touching the database, and (via the caller) pops up
# show_overlap_dialog() for any rows that failed any of these checks.
# ============================================================================
def save_refinery_mapping(edited_state, original_data):
    conn = st.connection("snowflake")
    session = conn.session()
    username = _get_username()
    changes_made = False
    overlap_messages = []

    edited_rows = edited_state.get("edited_rows", {})

    for str_idx, changes in edited_rows.items():
        idx = int(str_idx)
        orig_row = original_data.loc[idx]

        if not any(c in changes for c in ("Premium", "Start Date", "End Date")):
            continue

        mapping_id = int(orig_row["Ref Sup Mapping Id"])
        orig_start_dt = orig_row["Start Date"]
        orig_end_dt = orig_row["End Date"] if pd.notna(orig_row["End Date"]) else None
        orig_premium = orig_row.get("Premium")

        new_start_dt = changes.get("Start Date", orig_start_dt)
        new_end_dt = changes.get("End Date", orig_end_dt)
        new_premium = changes.get("Premium", orig_premium)

        new_val = parse_accounting(new_premium)
        old_val = parse_accounting(orig_premium)

        if new_val == old_val and str(new_start_dt) == str(orig_start_dt) and str(new_end_dt) == str(orig_end_dt):
            continue

        # ---- Start Date is mandatory — block and report rather than letting
        # the INSERT fail on the table's NOT NULL constraint ----
        if new_start_dt is None or pd.isna(new_start_dt) or str(new_start_dt).strip() == "":
            overlap_messages.append(
                f"{orig_row.get('Supplier','')} / {orig_row.get('Contract Type','')} / "
                f"{orig_row.get('Price Code','')}: Start Date cannot be blank. Not saved."
            )
            continue

        # ---- End Date must not be earlier than Start Date ----
        if new_end_dt is not None and not pd.isna(new_end_dt) and str(new_end_dt).strip() != "":
            if pd.to_datetime(new_end_dt) < pd.to_datetime(new_start_dt):
                overlap_messages.append(
                    f"{orig_row.get('Supplier','')} / {orig_row.get('Contract Type','')} / "
                    f"{orig_row.get('Price Code','')}: End Date ({new_end_dt}) cannot be earlier than "
                    f"Start Date ({new_start_dt}). Not saved."
                )
                continue

        if str(new_start_dt) != str(orig_start_dt) or str(new_end_dt) != str(orig_end_dt):
            if check_overlap(session, mapping_id, new_start_dt, new_end_dt, exclude_start_dt=orig_start_dt):
                overlap_messages.append(
                    f"{orig_row.get('Supplier','')} / {orig_row.get('Contract Type','')} / "
                    f"{orig_row.get('Price Code','')}: new dates ({new_start_dt} to {new_end_dt or 'open'}) "
                    f"overlap with another active period."
                )
                continue

        new_val_sql = new_val if new_val is not None else 'NULL'
        new_end_sql = f"'{new_end_dt}'" if new_end_dt else 'NULL'

        # ---- Only expire a prior version if one actually exists.
        # If orig_start_dt is blank, this row had no premium data at all
        # (a "ghost" placeholder row) — there's nothing to expire, just insert. ----
        if orig_start_dt is not None and not pd.isna(orig_start_dt) and str(orig_start_dt).strip() != "":
            session.sql(f"""
                UPDATE CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
                SET RECORD_END_TS = CURRENT_TIMESTAMP()
                WHERE REF_SUP_MAPPING_ID = {mapping_id}
                  AND EFFECTIVE_START_DT = '{orig_start_dt}'
                  AND RECORD_END_TS IS NULL
            """).collect()

        # 2. Insert the corrected version
        session.sql(f"""
            INSERT INTO CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
                (REF_SUP_MAPPING_ID, PREMIUM, EFFECTIVE_START_DT, EFFECTIVE_END_DT, RECORD_START_TS, RECORD_END_TS, UPDATED_BY)
            VALUES
                ({mapping_id}, {new_val_sql}, '{new_start_dt}', {new_end_sql}, CURRENT_TIMESTAMP(), NULL, '{username}')
        """).collect()

        changes_made = True

    if overlap_messages:
        st.session_state.show_overlap_popup = overlap_messages

    return changes_made


# ============================================================================
# NEW: add a brand-new forward-dated premium period for an existing mapping.
# Closes out the currently open-ended row (if any) and inserts the new one.
# Validates Start Date is present and End Date >= Start Date before checking
# for overlap. Takes an existing `session` so it can be called repeatedly in
# a loop when committing several pending periods at once.
# ============================================================================
def add_new_premium_period(session, mapping_id, new_start_dt, new_end_dt, new_premium, username):
    if new_start_dt is None or str(new_start_dt).strip() == "":
        return False, "Start Date cannot be blank."

    if new_end_dt is not None and str(new_end_dt).strip() != "":
        if pd.to_datetime(new_end_dt) < pd.to_datetime(new_start_dt):
            return False, f"End Date ({new_end_dt}) cannot be earlier than Start Date ({new_start_dt})."

    # ---- Find the currently open-ended row (if any) for this mapping ----
    open_row = session.sql(f"""
        SELECT EFFECTIVE_START_DT
        FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
        WHERE REF_SUP_MAPPING_ID = {mapping_id}
          AND EFFECTIVE_END_DT IS NULL
          AND RECORD_END_TS IS NULL
    """).collect()

    # If the new period starts AFTER the currently open-ended row, this call is
    # intentionally meant to supersede/truncate it — exclude it from the overlap
    # check (it will be closed out below), rather than treating it as a conflict.
    exclude_start_dt = None
    if open_row:
        open_start_dt = open_row[0]["EFFECTIVE_START_DT"]
        if pd.to_datetime(new_start_dt) > pd.to_datetime(open_start_dt):
            exclude_start_dt = str(open_start_dt)

    if check_overlap(session, mapping_id, new_start_dt, new_end_dt, exclude_start_dt=exclude_start_dt):
        return False, f"The new date range ({new_start_dt} to {new_end_dt or 'open'}) overlaps with an existing active period for this mapping."

    prior_end_dt = (pd.to_datetime(new_start_dt) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    session.sql(f"""
        UPDATE CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
        SET EFFECTIVE_END_DT = '{prior_end_dt}', RECORD_END_TS = CURRENT_TIMESTAMP()
        WHERE REF_SUP_MAPPING_ID = {mapping_id}
          AND EFFECTIVE_END_DT IS NULL
          AND RECORD_END_TS IS NULL
    """).collect()

    new_end_sql = f"'{new_end_dt}'" if new_end_dt else 'NULL'

    session.sql(f"""
        INSERT INTO CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM
            (REF_SUP_MAPPING_ID, PREMIUM, EFFECTIVE_START_DT, EFFECTIVE_END_DT, RECORD_START_TS, RECORD_END_TS, UPDATED_BY)
        VALUES
            ({mapping_id}, {new_premium}, '{new_start_dt}', {new_end_sql}, CURRENT_TIMESTAMP(), NULL, '{username}')
    """).collect()

    return True, None


if st.session_state.get("reset_confirm_mapping", False):
    if "confirm_mapping" in st.session_state:
        st.session_state.confirm_mapping = False
    st.session_state.reset_confirm_mapping = False

# --- Refinery Price Code Mapping Table ---
st.markdown("<h3 style='color: #38A169; font-size: 1.25rem;'>Refinery Price Code Mapping</h3>", unsafe_allow_html=True)



if period_locked:
    st.info("Editing is disabled for closed periods.")
elif view_only:
    st.info("You have view-only access for this refinery.")

cols_to_display = ["Refinery", "Supplier", "Contract Type", "Pricing Region", "Price Code", "Price Code Description", "Start Date", "End Date", "Premium"]
df_mapping_display = df_mapping.copy()
df_mapping_display["Premium"] = df_mapping_display["Premium"].map(lambda x: format_accounting(x, 4, is_currency=True))
# Ensure Start/End Date are real date values (not strings) so DateColumn renders a calendar picker
df_mapping_display["Start Date"] = pd.to_datetime(df_mapping_display["Start Date"], errors="coerce").dt.date
df_mapping_display["End Date"] = pd.to_datetime(df_mapping_display["End Date"], errors="coerce").dt.date

col_config = {}
for col in df_mapping_display.columns:
    if col not in cols_to_display:
        col_config[col] = None
    elif col == "Premium":
        col_config[col] = st.column_config.Column("Premium ($/MMBtu)", disabled=read_only)
    # ------------------------------------------------------------------
    # NEW: Start Date and End Date are now editable too (previously only
    # Premium was editable, since dates used to be overloaded audit columns).
    # ------------------------------------------------------------------
    elif col == "Start Date":
        col_config[col] = st.column_config.DateColumn("Start Date", format="YYYY-MM-DD", disabled=read_only)
    elif col == "End Date":
        col_config[col] = st.column_config.DateColumn("End Date", format="YYYY-MM-DD", disabled=read_only)
    else:
        col_config[col] = st.column_config.Column(disabled=True)

styled_mapping = df_mapping_display.style.set_properties(
    subset=["Premium"],
    **{'text-align': 'right'}
)

edited_mapping = st.data_editor(
    styled_mapping, 
    use_container_width=True, 
    hide_index=True, 
    disabled=read_only,
    column_config=col_config,
    column_order=cols_to_display,
    key="mapping_editor"
)

# --- Global Save Logic ---
if "confirm_changes_key_counter" not in st.session_state:
    st.session_state.confirm_changes_key_counter = 0
chk_key = f"confirm_all_changes_{st.session_state.confirm_changes_key_counter}"

# NEW: show overlap popup after rerun, same place other toasts get triggered
if "show_overlap_popup" in st.session_state:
    _overlap_msgs = st.session_state.show_overlap_popup
    del st.session_state["show_overlap_popup"]
    show_overlap_dialog(_overlap_msgs)

if not read_only:
    with check_ph.container():
        confirm_all = st.checkbox("Confirm Changes", key=chk_key)

    with save_ph.container():
        if st.button("Save", use_container_width=True, type="primary", disabled=not confirm_all, key="btn_save_all"):
            with st.spinner("Saving all changes..."):
                changed_mapping = save_refinery_mapping(st.session_state.get("mapping_editor", {}), df_mapping)

                # ---- Commit any pending "Add Period" entries staged via the popover ----
                pending = st.session_state.get("pending_new_periods", [])
                new_period_errors = []
                any_period_added = False

                if pending:
                    conn_p = st.connection("snowflake")
                    session_p = conn_p.session()
                    username_p = _get_username()

                    # Sort by mapping then start date, so if multiple periods were staged
                    # for the SAME mapping, they get applied in chronological order —
                    # each one correctly closes out the previously-committed one.
                    pending_sorted = sorted(pending, key=lambda e: (e["mapping_id"], e["start"]))

                    for entry in pending_sorted:
                        ok, err = add_new_premium_period(
                            session_p, entry["mapping_id"], entry["start"], entry["end"], entry["premium"], username_p
                        )
                        if ok:
                            any_period_added = True
                        else:
                            new_period_errors.append(f"{entry['label']}: {err}")

                    # Clear only the entries that were attempted; keep none regardless of outcome —
                    # failed ones are reported via the overlap popup so the admin can re-stage them.
                    st.session_state.pending_new_periods = []

                if new_period_errors:
                    existing = st.session_state.get("show_overlap_popup", [])
                    st.session_state.show_overlap_popup = existing + new_period_errors

            if changed_mapping or any_period_added:
                sp_sql = get_sp_burn_tracker_volume_call(month_year, refinery)
                if sp_sql:
                    try:
                        conn_sp = st.connection("snowflake")
                        conn_sp.session().sql(sp_sql).collect()
                    except Exception:
                        pass
                st.cache_data.clear()
                st.session_state.show_pricing_toast = "Changes saved successfully!"
                st.session_state.confirm_changes_key_counter += 1
                st.rerun()
            elif "show_overlap_popup" in st.session_state:
                st.session_state.confirm_changes_key_counter += 1
                st.rerun()
            else:
                st.session_state.show_pricing_toast = "No changes detected."
                st.session_state.confirm_changes_key_counter += 1
                st.rerun()


# ============================================================================
# NEW: "Add New Effective Period" expander.
# Uses st.expander instead of st.popover — nesting st.date_input inside
# st.popover has a known Streamlit bug (calendar dropdown doesn't render
# correctly / gets clipped, see streamlit/streamlit#13633). st.expander does
# not have this issue.
# The expander's own title is the only heading (no separate markdown header).
# Fields are laid out in one horizontal row to minimize vertical scrolling,
# and the identity info is shown as a single compact caption line (instead of
# a full read-only grid) to minimize both vertical and horizontal space.
# Submitting does NOT write to the database — it stages the entry into
# st.session_state.pending_new_periods. Nothing is actually saved until the
# main "Save" button (above) is clicked, which commits both grid edits and
# all staged periods together. Multiple periods can be staged for the SAME
# mapping — they're applied in start-date order at save time, so each one
# correctly closes out the period before it.
# ============================================================================
if "pending_new_periods" not in st.session_state:
    st.session_state.pending_new_periods = []

if not read_only and not df_mapping.empty:
    df_mapping_identity = df_mapping[
        ["Ref Sup Mapping Id", "Refinery", "Supplier", "Contract Type", "Pricing Region", "Price Code"]
    ].drop_duplicates(subset=["Ref Sup Mapping Id"]).reset_index(drop=True)

    df_mapping_identity["_label"] = (
        df_mapping_identity["Supplier"].astype(str) + " / " +
        df_mapping_identity["Contract Type"].astype(str) + " / " +
        df_mapping_identity["Pricing Region"].astype(str) + " / " +
        df_mapping_identity["Price Code"].astype(str)
    )

    st.markdown("""
    <style>
    div[data-testid="stExpander"] summary p {
        color: #38A169 !important;
        font-weight: 900;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border: 8px solid #38A169 !important;
        border-radius: 8px !important;
        padding: 8px 4px !important;
    }
    </style>
    """, unsafe_allow_html=True)

    if "new_period_widget_counter" not in st.session_state:
        st.session_state.new_period_widget_counter = 0
    _wc = st.session_state.new_period_widget_counter

    with st.expander("**Add New Effective Period**", expanded=False):
        # ---- Row 1: mapping selection + read-only identity confirmation, same line ----
        col_map, col_id = st.columns([1.9, 3])

        # Select by the actual Ref Sup Mapping Id (guaranteed unique), displaying the
        # readable label via format_func — avoids any risk of two different mappings
        # accidentally sharing the same displayed label string and being mismatched.
        _id_to_label = dict(zip(df_mapping_identity["Ref Sup Mapping Id"], df_mapping_identity["_label"]))
        mapping_ids_list = df_mapping_identity["Ref Sup Mapping Id"].tolist()

        with col_map:
            mapping_id_sel = st.selectbox(
                "Select Mapping",
                mapping_ids_list,
                format_func=lambda mid: _id_to_label.get(mid, str(mid)),
                key="new_period_mapping",
            )

        selected_identity_row = df_mapping_identity[df_mapping_identity["Ref Sup Mapping Id"] == mapping_id_sel].iloc[0]
        selected_label = _id_to_label.get(mapping_id_sel, "")

        with col_id:
            st.dataframe(
                pd.DataFrame([{
                    "Refinery": selected_identity_row["Refinery"],
                    "Supplier": selected_identity_row["Supplier"],
                    "Contract Type": selected_identity_row["Contract Type"],
                    "Pricing Region": selected_identity_row["Pricing Region"],
                    "Price Code": selected_identity_row["Price Code"],
                }]),
                use_container_width=True,
                hide_index=True,
            )

        # ---- Row 2: the 3 editable fields, each individually bordered, + Add button ----
        # Keys include _wc (widget counter) so incrementing it forces brand-new widget
        # instances with fresh defaults — deleting a widget's session_state key does NOT
        # reliably reset it in Streamlit, so a counter-suffixed key is used instead
        # (same pattern already used elsewhere in this app, e.g. confirm_changes_key_counter).
        col_start, col_end, col_prem, col_btn = st.columns([1, 1, 1, 1])
        with col_start:
            with st.container(border=True):
                _period_start_dt = pd.to_datetime(month_year, errors='coerce').replace(day=1).date()
                new_start_input = st.date_input("**Start Date**",value=_period_start_dt, format="YYYY-MM-DD", min_value=_period_start_dt, key=f"new_period_start_{_wc}")
                #new_start_input = st.date_input("**Start Date**", format="YYYY-MM-DD", key=f"new_period_start_{_wc}")
        with col_end:
            with st.container(border=True):
                new_end_input = st.date_input("**End Date**", value=None, format="YYYY-MM-DD", min_value=new_start_input, key=f"new_period_end_{_wc}")
                #new_end_input = st.date_input("**End Date**", value=None, format="YYYY-MM-DD", key=f"new_period_end_{_wc}")
        with col_prem:
            with st.container(border=True):
                new_premium_input = st.number_input("**Premium ($/MMBtu)**", format="%.4f", key=f"new_period_premium_{_wc}")
        with col_btn:
            st.markdown('<div style="margin-top: 28px;"></div>', unsafe_allow_html=True)
            add_clicked = st.button("Add to List", type="primary", use_container_width=True, key="btn_add_new_period")

        if add_clicked:
            start_str = new_start_input.strftime("%Y-%m-%d")
            end_str = new_end_input.strftime("%Y-%m-%d") if new_end_input else None

            # ---- Client-side validation before even staging the entry ----
            _validation_error = None
            if end_str is not None and pd.to_datetime(end_str) < pd.to_datetime(start_str):
                _validation_error = f"End Date ({end_str}) cannot be earlier than Start Date ({start_str})."

            if _validation_error:
                st.session_state.show_overlap_popup = [_validation_error]
                st.rerun()
            else:
                st.session_state.pending_new_periods.append({
                    "mapping_id": int(mapping_id_sel),
                    "label": f"{selected_identity_row['Refinery']} / {selected_label}",
                    "start": start_str,
                    "end": end_str,
                    "premium": new_premium_input,
                })
                # ---- Force the 3 input fields to reset by bumping the widget counter ----
                st.session_state.new_period_widget_counter += 1
                st.rerun()

        # ---- Preview of staged (not-yet-saved) periods ----
        if st.session_state.pending_new_periods:
            st.caption("Pending periods below will be saved when you click **Save**.")
            pending_df = pd.DataFrame(st.session_state.pending_new_periods)
            pending_df_display = pending_df.rename(columns={
                "label": "Mapping", "start": "Start Date", "end": "End Date", "premium": "Premium"
            })[["Mapping", "Start Date", "End Date", "Premium"]]
            pending_df_display.insert(0, "Remove", False)

            edited_pending = st.data_editor(
                pending_df_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Remove": st.column_config.CheckboxColumn("Remove", default=False, width=70),
                    "Mapping": st.column_config.Column(disabled=True),
                    "Start Date": st.column_config.Column(disabled=True),
                    "End Date": st.column_config.Column(disabled=True),
                    "Premium": st.column_config.Column(disabled=True),
                },
                key="pending_periods_editor",
            )

            if st.button("Remove Checked Rows", key="btn_remove_pending"):
                keep_indices = edited_pending[edited_pending["Remove"] != True].index.tolist()
                st.session_state.pending_new_periods = [
                    st.session_state.pending_new_periods[i] for i in keep_indices
                ]
                st.rerun()
else:
    st.caption("Add New Period is unavailable in view-only mode or when the period is closed.")
