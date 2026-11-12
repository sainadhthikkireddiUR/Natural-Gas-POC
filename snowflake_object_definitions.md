# Snowflake Object Definitions

**Generated:** 2026-11-12 23:10:43

**Account:** HFSINCLAIR-DEV  |  **Role:** DEVELOPER_FR

---

## 1. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_DASHBOARD_FILTER_VW`

**Type:** VIEW  

**Purpose:** View – Populates top filter dropdowns (Period, Refinery, Status)


```sql
create or replace view NG_DASHBOARD_FILTER_VW(
	CALENDAR_YEAR,
	MONTH_NUMBER,
	MONTH_NAME,
	FIRST_OF_MONTH,
	LAST_DAY_OF_MONTH,
	PERIOD,
	REFINERY_ID,
	REFINERY,
	SUPPLIER_ID,
	SUPPLIER,
	STATUS_ID,
	STATUS,
	"Last updated On",
	"Last updated By"
) as

SELECT DISTINCT

    dd.calendar_year,

    dd.month_number,

    dd.month_name,

    dd.first_of_month,

    dd.last_day_of_month,

    dd.MONTH_NAME || ' ' || dd.CALENDAR_YEAR AS PERIOD,

    ref.config_id   AS REFINERY_ID,

    ref.VALUE       AS REFINERY,

    rsm.SUPPLIER_ID,

    sup.VALUE       AS SUPPLIER,

    s.STATUS_ID,

    sta.VALUE       AS STATUS,

    s.STATUS_UPDATED_DT AS "Last updated On",

    s.STATUS_UPDATED_BY AS "Last updated By"



FROM (

    SELECT DISTINCT calendar_year, month_number, month_name, first_of_month, last_day_of_month

    FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_DATE_DIM

) dd



-- Only active refineries

JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG ref

    ON  ref.TYPE = 'REFINERY'

    AND ref.VALUE <> 'All'

    AND ref.RECORD_END_TS IS NULL



-- Only supplier-refinery contracts active for that month

LEFT JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUPPLIER_MAPPING rsm

    ON  rsm.REFINERY_ID = ref.CONFIG_ID

    AND rsm.RECORD_START_TS <= dd.first_of_month

    AND (rsm.RECORD_END_TS IS NULL OR rsm.RECORD_END_TS > dd.first_of_month)



-- Get supplier name active for that month (date-range match, DISTINCT handles dedup)

LEFT JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG sup

    ON  sup.CONFIG_ID = rsm.SUPPLIER_ID

    AND sup.TYPE = 'SUPPLIER'

    AND sup.RECORD_START_TS <= dd.first_of_month

    AND (sup.RECORD_END_TS IS NULL OR sup.RECORD_END_TS > dd.first_of_month)



-- Status for that refinery/month

INNER JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MONTHLY_STATUS s

    ON  s.REFINERY_ID  = ref.CONFIG_ID

    AND s.PERIOD_MONTH = dd.MONTH_NUMBER

    AND s.PERIOD_YEAR  = dd.CALENDAR_YEAR

    AND s.RECORD_END_TS IS NULL



LEFT JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG sta

    ON  sta.CONFIG_ID = s.STATUS_ID

    AND UPPER(sta.TYPE) = 'STATUS'



ORDER BY 1, 2, REFINERY, SUPPLIER

;
```

---

## 2. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG`

**Type:** TABLE  

**Purpose:** Table – Resolves CONFIG_ID (Refinery ID) from refinery name


```sql
create or replace TABLE NG_MASTER_CONFIG (
	CONFIG_ID NUMBER(38,0) NOT NULL,
	TYPE VARCHAR(16777216) NOT NULL,
	VALUE VARCHAR(16777216) NOT NULL,
	ADDITIONAL_ATTR_1 VARCHAR(16777216),
	ADDITIONAL_ATTR_2 VARCHAR(16777216),
	RECORD_START_TS TIMESTAMP_NTZ(9) NOT NULL,
	RECORD_END_TS TIMESTAMP_NTZ(9),
	UPDATED_BY VARCHAR(16777216) NOT NULL,
	UPDATED_AT TIMESTAMP_NTZ(9),
	primary key (CONFIG_ID)
);
```

---

## 3. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_PRICING_MASTER_VW`

**Type:** VIEW  

**Purpose:** View – Source of index market prices (FOM / Gas Daily)


```sql
create or replace view NG_PRICING_MASTER_VW(
	PRICE_QUOTE_ID,
	SYMBOL,
	DESCRIPTION,
	PRICE,
	PRICE_DATE,
	CONTRACT_TYPE
) COMMENT='This view provides pricing for symbols required for natural gas application. Manual edits in NG_PRICE_EDITS take priority over market prices.'
 as
WITH NG_PRICE_CODE AS (
    -- Deduplicate market prices: latest ingested record per symbol per day
    SELECT c.CONFIG_ID
        ,REPLACE(UPPER(q.symbol), '#', '') AS symbol
        ,q.description
        ,q.close AS price
        ,date(q.quotedatetime) AS price_date
        ,q.created_at
        ,c.ADDITIONAL_ATTR_1
        ,c.ADDITIONAL_ATTR_2
    FROM CONSUMPTION.HFS_COMMERCIAL_DATA.HISTORICAL_PRICE_QUOTES q
    INNER JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_DATE_DIM NDD
        ON NDD.CALENDAR_DATE = date(q.quotedatetime)
    INNER JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_MASTER_CONFIG c
        ON c.TYPE = 'PRICE_CODES'
        AND REPLACE(UPPER(q.symbol), '#', '') = C.VALUE
)
-- SELECT * FROM NG_PRICE_CODE;
,latest_quotes AS (
    SELECT * 
    FROM NG_PRICE_CODE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY symbol, price_date ORDER BY created_at DESC) = 1
)
-- select * from latest_quotes;
SELECT 
    q.CONFIG_ID AS PRICE_QUOTE_ID,
    q.SYMBOL,
    q.ADDITIONAL_ATTR_1 AS DESCRIPTION,
    q.price AS PRICE,
    q.price_date AS PRICE_DATE,
    q.ADDITIONAL_ATTR_2 AS CONTRACT_TYPE
from latest_quotes q;
```

---

## 4. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_PRICE_CODE_MAPPING_VW`

**Type:** VIEW  

**Purpose:** View – Refinery supplier price codes, contract types, premiums, effective dates


```sql
create or replace view NG_REFINERY_PRICE_CODE_MAPPING_VW(
	CALENDAR_DATE,
	REFINERY,
	PERIOD,
	SUPPLIER,
	CONTRACT_TYPE,
	PRICING_REGION,
	PRICE_CODE,
	PRICE_CODE_DESC,
	REF_SUP_MAPPING_ID,
	REFINERY_ID,
	SUPPLIER_ID,
	CONTRACT_TYPE_ID,
	PRICING_REGION_ID,
	PRICE_CODE_ID,
	START_DATE,
	END_DATE,
	PREMIUM
) as

SELECT DISTINCT

    v.CALENDAR_DATE,

    v.REFINERY_NAME                                         AS REFINERY,

    v.PERIOD,

    v.SUPPLIER_NAME                                         AS SUPPLIER,

    v.CONTRACT_TYPE_NAME                                    AS CONTRACT_TYPE,

    v.PRICING_REGION_NAME                                   AS PRICING_REGION,

    v.PRICE_CODE,

    v.PRICE_CODE_DESC,

    v.REF_SUP_MAPPING_ID,

    v.REFINERY_ID,

    v.SUPPLIER_ID,

    v.CONTRACT_TYPE_ID,

    v.PRICING_REGION_ID,

    v.PRICE_CODE_ID,

    p.EFFECTIVE_START_DT                                    AS START_DATE,

    p.EFFECTIVE_END_DT                                      AS END_DATE,

    COALESCE(p.PREMIUM, v.PREMIUM)                         AS PREMIUM

FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REF_SUPPLIER_MAPPING_VW v

LEFT JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM p

    ON  v.REF_SUP_MAPPING_ID = p.REF_SUP_MAPPING_ID

    AND p.RECORD_END_TS IS NULL

    AND v.CALENDAR_DATE BETWEEN DATE(p.EFFECTIVE_START_DT)

                             AND COALESCE(DATE(p.EFFECTIVE_END_DT), '9999-12-31');
```

---

## 5. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUP_PREMIUM`

**Type:** TABLE  

**Purpose:** Table – Target table for premium edits (UPDATE to expire, INSERT new rows)


```sql
create or replace TABLE NG_REFINERY_SUP_PREMIUM (
	REF_SUP_MAPPING_ID NUMBER(38,0) NOT NULL,
	PREMIUM NUMBER(18,4) NOT NULL,
	EFFECTIVE_START_DT DATE NOT NULL,
	EFFECTIVE_END_DT DATE,
	RECORD_START_TS TIMESTAMP_NTZ(9) NOT NULL,
	RECORD_END_TS TIMESTAMP_NTZ(9),
	UPDATED_BY VARCHAR(16777216) NOT NULL,
	primary key (REF_SUP_MAPPING_ID, EFFECTIVE_START_DT)
);
```

---

## 6. `CONSUMPTION.HFS_COMMERCIAL_ONLY.SP_BURN_TRACKER_VOLUME`

**Type:** PROCEDURE  

**Purpose:** Stored Procedure – Recalculates burn tracker costs/volumes after save


```sql
CREATE OR REPLACE PROCEDURE "SP_BURN_TRACKER_VOLUME"("P_REFINERY_ID" NUMBER(38,0) DEFAULT null, "P_PERIOD_MONTH" NUMBER(38,0) DEFAULT null, "P_PERIOD_YEAR" NUMBER(38,0) DEFAULT null)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
    V_MESSAGE TEXT;
    V_PERIOD INTEGER;
BEGIN
    -- 1. DELETE existing records for the specified refinery and period (or all if NULL)
    DELETE FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_TRACKER_VOLUME_CALCS
    WHERE (:P_REFINERY_ID IS NULL  OR REFINERY_ID = :P_REFINERY_ID)
      AND (:P_PERIOD_MONTH IS NULL OR MONTH_NUMBER = :P_PERIOD_MONTH)
      AND (:P_PERIOD_YEAR IS NULL  OR CALENDAR_YEAR = :P_PERIOD_YEAR);
    
    -- Update CASPER from values on 4050
    CREATE OR REPLACE TEMPORARY TABLE CONSUMPTION.HFS_COMMERCIAL_ONLY.PARCO_CASPER_METER_READING_CALC
    AS
    SELECT MM.*, RR.DAILY_BURN_READING
    FROM (SELECT *
        FROM CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_METER_READING R
        WHERE R.RECORD_END_TS IS NULL
        AND REFINERY_ID = 9 --PARCO REFINERY
        AND METER_NUMBER = ''4050''
        -- AND r.calendar_date = ''2026-04-12''
        ) RR
    JOIN (SELECT * FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REF_SUPPLIER_MAPPING_VW
        WHERE REF_SUP_MAPPING_ID = 7
        -- AND calendar_date = ''2026-04-12''
    ) MM
    ON RR.CALENDAR_DATE = MM.CALENDAR_DATE 
    -- AND MM.REFINERY_ID = :P_REFINERY_ID
    -- AND MM.MONTH_NUMBER = :P_PERIOD_MONTH
    -- AND MM.CALENDAR_YEAR = :P_PERIOD_YEAR
    AND (:P_REFINERY_ID IS NULL  OR MM.REFINERY_ID = :P_REFINERY_ID)
    AND (:P_PERIOD_MONTH IS NULL OR MM.MONTH_NUMBER = :P_PERIOD_MONTH)
    AND (:P_PERIOD_YEAR IS NULL  OR MM.CALENDAR_YEAR = :P_PERIOD_YEAR)
    ;

    
    CREATE OR REPLACE TEMPORARY TABLE VOL_CALC
    AS
    SELECT V.* REPLACE( COALESCE(V.BUY_VOLUME,0) - COALESCE(W.DAILY_BURN_READING,0) AS BUY_VOLUME), W.DAILY_BURN_READING
    FROM CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME V
    LEFT OUTER JOIN PARCO_CASPER_METER_READING_CALC W
        ON V.REF_SUP_MAPPING_ID = W.REF_SUP_MAPPING_ID
        AND V.CALENDAR_DATE = W.CALENDAR_DATE
    WHERE V.RECORD_END_TS IS NULL
    AND (:P_PERIOD_MONTH IS NULL OR MONTH(V.CALENDAR_DATE) = :P_PERIOD_MONTH)
    AND (:P_PERIOD_YEAR IS NULL  OR YEAR(V.CALENDAR_DATE) = :P_PERIOD_YEAR)
    -- AND V.calendar_date = ''2026-04-12''
    ;

    IF (:P_REFINERY_ID IS NULL OR :P_REFINERY_ID = 9)
    THEN
    -- update volume for Supplier "United Energy Trading - Uet" for Parco refinery, using meter readings for Parco meter number, 4050 
        UPDATE CONSUMPTION.HFS_COMMERCIAL_ONLY.VOL_CALC vc
        SET BUY_VOLUME = mr.daily_burn_reading
        FROM CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_METER_READING mr
        WHERE vc.calendar_date = mr.calendar_date
            AND vc.ref_sup_mapping_id = (
                select distinct ref_sup_mapping_id
                from CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REF_SUPPLIER_MAPPING_VW
                where upper(supplier_name) = ''UNITED - UET 4050''
                ) --- ref_supplier_mapping_id for supplier "United Energy Trading - Uet" with  "Gas Daily" contract type
            AND mr.record_end_ts IS NULL
            AND mr.refinery_id = 9 --PARCO REFINERY
            AND mr.meter_number = ''4050''
        ;

        -- Update meter readings for CIG in PARCO: CIG_BASE (FOM), CIG_SWING (GD excl. 4050), CIG_BALANCE = ACTUAL - BASE - SWING
        create or replace temporary table cig_calcs
        AS 
        with base_data as (
        SELECT
                vc.CALENDAR_DATE,
                SUM(IFF(sm.CONTRACT_TYPE_NAME = ''First of Month'', vc.BUY_VOLUME, 0)) AS CIG_BASE,
                SUM(IFF(sm.CONTRACT_TYPE_NAME = ''Gas Daily'',
                        vc.BUY_VOLUME, 0))                                              AS CIG_SWING,
                MAX(mr2.daily_burn_reading) AS METER_4050 
                --   - SUM(IFF(sm.CONTRACT_TYPE_NAME = ''First of Month'', vc.BUY_VOLUME, 0))
                --   - SUM(IFF(sm.CONTRACT_TYPE_NAME = ''Gas Daily''
                --             AND UPPER(sm.SUPPLIER_NAME) != ''UNITED - UET 4050'',
                --             vc.BUY_VOLUME, 0))                                          AS CIG_BALANCE
            FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.VOL_CALC vc
            JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REF_SUPPLIER_MAPPING_VW sm
                ON  sm.REF_SUP_MAPPING_ID = vc.REF_SUP_MAPPING_ID
                AND sm.CALENDAR_DATE      = vc.CALENDAR_DATE
            LEFT JOIN CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_METER_READING mr2
                ON  mr2.CALENDAR_DATE = vc.CALENDAR_DATE
                AND mr2.REFINERY_ID   = 9
                AND mr2.METER_NUMBER  = ''ACTUAL''
                AND mr2.RECORD_END_TS IS NULL
                
            WHERE sm.REFINERY_ID = 9
            AND UPPER(sm.SUPPLIER_NAME) != ''UNITED - UET 4050''
            GROUP BY vc.CALENDAR_DATE
            )
            select * ,METER_4050-CIG_BASE-CIG_SWING as CIG_BALANCE from base_data;

        UPDATE CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_METER_READING mr
        SET daily_burn_reading =
            CASE mr.meter_number
                WHEN ''CIG_BASE''    THEN calc.CIG_BASE
                WHEN ''CIG_SWING''   THEN calc.CIG_SWING
                WHEN ''CIG_BALANCE'' THEN calc.CIG_BALANCE
            END
        FROM cig_calcs  calc     
        
        WHERE mr.CALENDAR_DATE = calc.CALENDAR_DATE
          AND mr.REFINERY_ID   = 9
          AND mr.METER_NUMBER  IN (''CIG_BASE'', ''CIG_SWING'', ''CIG_BALANCE'')
          AND mr.RECORD_END_TS IS NULL;

    END IF;

 
 -- 2. RECALCULATE and INSERT records for the specified refinery and period
    INSERT INTO CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_TRACKER_VOLUME_CALCS (
        CALENDAR_DATE,
        CALENDAR_YEAR,
        MONTH_NUMBER,
        MONTH_NAME,
        DAY_OF_MONTH,
        REF_SUP_MAPPING_ID,
        REFINERY_ID,
        TYPE,
        REFINERY_NAME,
        SUPPLIER_ID,
        SUPPLIER_NAME,
        CONTRACT_TYPE_ID,
        CONTRACT_TYPE_NAME,
        PRICING_REGION_ID,
        PRICING_REGION_NAME,
        PRICE_CODE_ID,
        PREMIUM,
        PREM_START_DATE,
        PREM_END_DATE,
        PERIOD,
        BASE_BUY_VOLUME,
        TRANSACTION_VOLUME,
        VOLUME,
        TRANSACTION_AMOUNT,
        BV_START_DATE,
        BV_END_DATE,
        SYMBOL,
        PRICE,
        PRICE_DATE,
        ALL_IN_PRICE,
        COST
    )
    
    WITH ref_mapping_detail AS (
        SELECT 
            v.CALENDAR_DATE,
            YEAR(v.CALENDAR_DATE)      AS CALENDAR_YEAR,
            MONTH(v.CALENDAR_DATE)     AS MONTH_NUMBER,
            MONTHNAME(v.CALENDAR_DATE) AS MONTH_NAME,
            DAY(v.CALENDAR_DATE)       AS DAY_OF_MONTH,
            v.REF_SUP_MAPPING_ID,
            v.REFINERY_ID,
            ''REFINERY''                AS TYPE,
            v.REFINERY                 AS REFINERY_NAME,
            v.SUPPLIER_ID,
            v.SUPPLIER                 AS SUPPLIER_NAME,
            v.CONTRACT_TYPE_ID,
            v.CONTRACT_TYPE            AS CONTRACT_TYPE_NAME,
            v.PRICING_REGION_ID,
            v.PRICING_REGION           AS PRICING_REGION_NAME,
            v.PRICE_CODE_ID,
            coalesce(v.PREMIUM,0) AS PREMIUM,
            coalesce(v.START_DATE, ''1900-01-01'')                AS PREM_START_DATE,
            coalesce(v.END_DATE, ''9999-01-01'')                  AS PREM_END_DATE,
            v.PERIOD,
            REPLACE(v.SUPPLIER, '' '', ''_'') || ''_'' ||
            REPLACE(v.CONTRACT_TYPE, '' '', ''_'') || ''_'' ||
            REPLACE(v.PRICING_REGION, '' '', ''_'') AS COMBINATION_KEY
        FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_PRICE_CODE_MAPPING_VW v
        WHERE (:P_REFINERY_ID IS NULL  OR v.REFINERY_ID = :P_REFINERY_ID)
          AND (:P_PERIOD_MONTH IS NULL OR MONTH(v.CALENDAR_DATE) = :P_PERIOD_MONTH)
          AND (:P_PERIOD_YEAR IS NULL  OR YEAR(v.CALENDAR_DATE) = :P_PERIOD_YEAR)
    ),

    nat AS (
        SELECT 
            CALENDAR_DATE, 
            REF_SUP_MAPPING_ID, 
            TRANSACTION_TYPE,
            SUM(TRANSACTION_VOLUME) AS TRANSACTION_VOLUME,
            SUM(AMOUNT)             AS AMOUNT,
            MAX(TRANSACTION_PRICE)  AS TRANSACTION_PRICE
        FROM CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_ADDITIONAL_TRANSACTION
        WHERE RECORD_END_TS IS NULL
        GROUP BY CALENDAR_DATE, REF_SUP_MAPPING_ID, TRANSACTION_TYPE
    )

    SELECT distinct
        RFD.CALENDAR_DATE,
        RFD.CALENDAR_YEAR,
        RFD.MONTH_NUMBER,
        RFD.MONTH_NAME,
        RFD.DAY_OF_MONTH,
        RFD.REF_SUP_MAPPING_ID,
        RFD.REFINERY_ID,
        RFD.TYPE,
        RFD.REFINERY_NAME,
        RFD.SUPPLIER_ID,
        RFD.SUPPLIER_NAME,
        RFD.CONTRACT_TYPE_ID,
        RFD.CONTRACT_TYPE_NAME,
        RFD.PRICING_REGION_ID,
        RFD.PRICING_REGION_NAME,
        RFD.PRICE_CODE_ID,
        RFD.PREMIUM,
        RFD.PREM_START_DATE,
        RFD.PREM_END_DATE,
        RFD.PERIOD,
        COALESCE(bv.BUY_VOLUME, 0)                AS BASE_BUY_VOLUME,
        COALESCE(nat.TRANSACTION_VOLUME, 0)       AS TRANSACTION_VOLUME,
        CASE 
            WHEN UPPER(nat.TRANSACTION_TYPE) = ''SELL'' THEN COALESCE(bv.BUY_VOLUME, 0) - COALESCE(nat.TRANSACTION_VOLUME, 0)
            WHEN UPPER(nat.TRANSACTION_TYPE) = ''BUY''  THEN COALESCE(bv.BUY_VOLUME, 0) + COALESCE(nat.TRANSACTION_VOLUME, 0)
            ELSE COALESCE(bv.BUY_VOLUME, 0)
        END                                       AS VOLUME, 
        COALESCE(nat.AMOUNT, 0)                   AS TRANSACTION_AMOUNT,
        DATE(bv.RECORD_START_TS)                  AS BV_START_DATE,
        DATE(bv.RECORD_END_TS)                    AS BV_END_DATE,
        prc.SYMBOL,
        COALESCE(prc.PRICE, 0)                    AS PRICE,
        prc.PRICE_DATE,
        COALESCE(prc.PRICE, 0) + COALESCE(RFD.PREMIUM, 0) AS ALL_IN_PRICE,
        CASE 
            WHEN UPPER(nat.TRANSACTION_TYPE) = ''SELL'' 
                THEN (COALESCE(bv.BUY_VOLUME, 0) * (COALESCE(prc.PRICE, 0) + COALESCE(RFD.PREMIUM, 0))) - COALESCE(nat.AMOUNT, 0)
            WHEN UPPER(nat.TRANSACTION_TYPE) = ''BUY'' 
                THEN (COALESCE(bv.BUY_VOLUME, 0) * (COALESCE(prc.PRICE, 0) + COALESCE(RFD.PREMIUM, 0))) + COALESCE(nat.AMOUNT, 0)
            ELSE COALESCE(bv.BUY_VOLUME, 0) * (COALESCE(prc.PRICE, 0) + COALESCE(RFD.PREMIUM, 0))
        END                                       AS COST

    FROM ref_mapping_detail RFD

    LEFT OUTER JOIN VOL_CALC bv
        ON RFD.REF_SUP_MAPPING_ID = bv.REF_SUP_MAPPING_ID
       AND RFD.CALENDAR_DATE      = bv.CALENDAR_DATE

    LEFT JOIN CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_PRICING_MASTER_VW prc
        ON prc.PRICE_QUOTE_ID = RFD.PRICE_CODE_ID
       AND (
            (prc.PRICE_DATE = RFD.CALENDAR_DATE AND prc.CONTRACT_TYPE = ''Gas Daily'')
            OR 
            (DATE_TRUNC(''MONTH'', prc.PRICE_DATE) = DATE_TRUNC(''MONTH'', RFD.CALENDAR_DATE) AND (prc.CONTRACT_TYPE = ''FOM'' OR prc.CONTRACT_TYPE = ''First of Month''))
           )

    LEFT JOIN nat
        ON nat.REF_SUP_MAPPING_ID = RFD.REF_SUP_MAPPING_ID
       AND nat.CALENDAR_DATE      = RFD.CALENDAR_DATE;    


    SELECT ''NG_BURN_TRACKER_VOLUME_CALCS loaded successfully for refinery: '' 
        || COALESCE(:P_REFINERY_ID::text, ''All'') 
        || '' and period: '' 
        || coalesce(lpad(:P_PERIOD_MONTH,2,''0'') || :P_PERIOD_YEAR, ''All'')
    INTO V_MESSAGE
    -- from (select null::integer P_REFINERY_ID, null P_PERIOD_YEAR, null P_PERIOD_MONTH)
    ;

    RETURN :V_MESSAGE;
END;
';
```

---

## 7. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_BURN_TRACKER_VOLUME_CALCS`

**Type:** VIEW  

**Purpose:** Table/View – Burn tracker volume calculations


```sql
create or replace TABLE NG_BURN_TRACKER_VOLUME_CALCS (
	CALENDAR_DATE DATE,
	CALENDAR_YEAR NUMBER(4,0),
	MONTH_NUMBER NUMBER(2,0),
	MONTH_NAME VARCHAR(3),
	DAY_OF_MONTH NUMBER(2,0),
	REF_SUP_MAPPING_ID NUMBER(38,0),
	REFINERY_ID NUMBER(38,0),
	TYPE VARCHAR(16777216),
	REFINERY_NAME VARCHAR(16777216),
	SUPPLIER_ID NUMBER(38,0),
	SUPPLIER_NAME VARCHAR(16777216),
	CONTRACT_TYPE_ID NUMBER(38,0),
	CONTRACT_TYPE_NAME VARCHAR(16777216),
	PRICING_REGION_ID NUMBER(38,0),
	PRICING_REGION_NAME VARCHAR(16777216),
	PRICE_CODE_ID NUMBER(38,0),
	PREMIUM NUMBER(18,4),
	PREM_START_DATE DATE,
	PREM_END_DATE DATE,
	PERIOD VARCHAR(16777216),
	BASE_BUY_VOLUME NUMBER(38,0),
	TRANSACTION_VOLUME NUMBER(22,5),
	VOLUME NUMBER(38,5),
	TRANSACTION_AMOUNT NUMBER(38,10),
	BV_START_DATE DATE,
	BV_END_DATE DATE,
	SYMBOL VARCHAR(16777216),
	PRICE NUMBER(20,4),
	PRICE_DATE DATE,
	ALL_IN_PRICE NUMBER(21,4),
	COST NUMBER(38,10)
);
```

---

## 8. `CONSUMPTION.HFS_COMMERCIAL_ONLY.NG_REFINERY_SUPPLIER_MAPPING`

**Type:** VIEW  

**Purpose:** Table/View – Refinery supplier mapping


```sql
create or replace TABLE NG_REFINERY_SUPPLIER_MAPPING (
	REF_SUP_MAPPING_ID NUMBER(38,0) NOT NULL,
	REFINERY_ID NUMBER(38,0) NOT NULL,
	SUPPLIER_ID NUMBER(38,0) NOT NULL,
	CONTRACT_TYPE_ID NUMBER(38,0) NOT NULL,
	PRICING_REGION_ID NUMBER(38,0) NOT NULL,
	PRICE_CODE_ID NUMBER(38,0) NOT NULL,
	PREMIUM NUMBER(18,4) NOT NULL,
	RECORD_START_TS TIMESTAMP_NTZ(9) NOT NULL,
	RECORD_END_TS TIMESTAMP_NTZ(9),
	UPDATED_BY VARCHAR(16777216) NOT NULL,
	primary key (REF_SUP_MAPPING_ID)
);
```

---

## 9. `CONFORMED.HFS_COMMERCIAL_ONLY.NG_REFINERY_BUY_VOLUME`

**Type:** VIEW  

**Purpose:** Table/View – Refinery buy volume (CONFORMED database)


```sql
create or replace TABLE NG_REFINERY_BUY_VOLUME (
	REF_SUP_MAPPING_ID NUMBER(38,0) NOT NULL,
	CALENDAR_DATE DATE NOT NULL,
	BUY_VOLUME NUMBER(38,0),
	RECORD_START_TS TIMESTAMP_NTZ(9) NOT NULL,
	RECORD_END_TS TIMESTAMP_NTZ(9),
	UPDATED_BY VARCHAR(16777216) NOT NULL
);
```

---
