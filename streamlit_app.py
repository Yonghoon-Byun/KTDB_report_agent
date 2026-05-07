import io
import re

import google.generativeai as genai
import pandas as pd
import psycopg2
import streamlit as st

from db_env import connect as azure_connect
from db_env import get_config as azure_get_config


st.set_page_config(page_title="KTDB 통합 분석 에이전트", layout="wide")

st.markdown(
    """
<style>
thead tr th { background: #f0f2f6; font-weight: 600; }
.stDataFrame { font-size: 13px; }
</style>
""",
    unsafe_allow_html=True,
)


YEARS = ["2023", "2025", "2030", "2035", "2040", "2045", "2050"]
DIST_YEARS = [int(y) for y in YEARS]
YEAR_COLUMN_RE = re.compile(r"^\d{4}년$")
KEY_COLUMNS_KR = ["시도", "시군구", "존번호", "발생존", "도착존", "도착시도", "도착시군구"]

BACKEND_LABELS = {
    "azure": "Azure Postgres ktdb (smr_*)",
    "legacy": "Legacy Supabase (fallback)",
}

PURPOSE_CODE_TO_KR = {"HOME": "귀가", "WORK": "출근", "SCHO": "등교", "OTHE": "기타"}
PURPOSE_KR_TO_CODE = {v: k for k, v in PURPOSE_CODE_TO_KR.items()}

AZURE_DATASETS = {
    "사회경제지표": {
        "tabs": {
            "ZONE": "존체계(1310 TAZ)",
            "POP_TOT": "총 인구수",
            "POP_YNG": "5-24세 인구수",
            "POP_15P": "15세이상 인구수",
            "EMP": "취업자수",
            "STU": "학생수",
            "WORK_TOT": "종사자수",
        }
    },
    "목적OD": {
        "tabs": {f"PUR_{y}": f"목적OD ({y}년)" for y in YEARS}
    },
    "주수단OD": {
        "tabs": {f"MOD_{y}": f"주수단OD ({y}년)" for y in YEARS}
    },
    "목적별주수단OD": {
        "tabs": {
            f"PURMOD_{p}_{y}": f"{PURPOSE_CODE_TO_KR[p]}({y}년) 주수단"
            for p in ["HOME", "WORK", "SCHO", "OTHE"]
            for y in YEARS
        }
    },
}

LEGACY_DATASETS = {
    "사회경제지표": {
        "tabs": {
            "ZONE": "존체계(행정구역)",
            "POP_TOT": "총 인구수",
            "POP_YNG": "5-24세 인구수",
            "POP_15P": "15세이상 인구수",
            "EMP": "취업자수",
            "STU": "수용학생수",
            "WORK_TOT": "종사자수",
        }
    },
    "목적OD": {
        "tabs": {f"PUR_{y}": f"목적OD ({y}년)" for y in YEARS}
    },
    "주수단OD": {
        "tabs": {f"MOD_{y}": f"주수단OD ({y}년)" for y in YEARS}
    },
    "접근수단OD": {
        "tabs": {"ATTMOD_2023": "접근수단OD (2023년)"}
    },
}

COL_KR = {
    "SIDO": "시도",
    "SIGU": "시군구",
    "ZONE": "존번호",
    "ORGN": "발생존",
    "DEST": "도착존",
    "DEST_SIDO": "도착시도",
    "DEST_SIGU": "도착시군구",
    "2023": "2023년",
    "2025": "2025년",
    "2030": "2030년",
    "2035": "2035년",
    "2040": "2040년",
    "2045": "2045년",
    "2050": "2050년",
    "WORK": "출근",
    "SCHO": "등교",
    "BUSI": "업무",
    "HOME": "귀가",
    "OTHE": "기타",
    "AUTO": "승용차",
    "BUS": "버스",
    "OBUS": "버스",
    "SUBW": "지하철",
    "RAIL": "일반철도",
    "ERAI": "고속철도",
    "KTX": "KTX",
    "WALK_BIKE": "도보/자전거",
    "FREIGHT": "화물/기타",
    "ETC_BUS": "기타버스",
    "TAXI": "택시",
    "BUS_SUBW": "버스+지하철",
    "ATT_AANT": "승용차(접근)",
    "ATT_OBUS": "버스(접근)",
}

UNITS = {
    "사회경제지표": "명",
    "목적OD": "통행/일",
    "주수단OD": "통행/일",
    "목적별주수단OD": "통행/일",
    "접근수단OD": "통행/일",
}

OD_QUERY_HINTS = {
    "목적OD": {
        "귀가": "귀가",
        "출근": "출근",
        "등교": "등교",
        "업무": "업무",
        "기타": "기타",
    },
    "주수단OD": {
        "도보": "도보/자전거",
        "자전거": "도보/자전거",
        "화물": "화물/기타",
        "기타버스": "기타버스",
        "철도": "일반철도",
        "일반철도": "일반철도",
        "고속철도": "KTX",
        "ktx": "KTX",
        "승용차": "승용차",
        "자동차": "승용차",
        "택시": "택시",
        "버스+지하철": "버스+지하철",
        "버스": "버스",
        "지하철": "지하철",
    },
    "목적별주수단OD": {
        "도보": "도보/자전거",
        "자전거": "도보/자전거",
        "화물": "화물/기타",
        "기타버스": "기타버스",
        "철도": "일반철도",
        "일반철도": "일반철도",
        "고속철도": "KTX",
        "ktx": "KTX",
        "승용차": "승용차",
        "자동차": "승용차",
        "택시": "택시",
        "버스+지하철": "버스+지하철",
        "버스": "버스",
        "지하철": "지하철",
    },
    "접근수단OD": {
        "승용차": "승용차(접근)",
        "버스": "버스(접근)",
        "접근": "승용차(접근)",
    },
}


# Reserved for future natural-language summary feature.
# 결과 표 생성·집계는 모두 SQL/pandas 결정론적 경로로 처리한다 (절대 룰: hallucination 금지).
# 향후 결과 표 + 파라미터를 LLM 에 넘겨 "한 단락 자연어 해설"을 추가할 때만 본 model 을 사용.
@st.cache_resource
def init_model():
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
    except Exception:
        return None
    try:
        genai.configure(api_key=api_key)
        return genai.GenerativeModel("gemini-2.5-pro")
    except Exception:
        return None


model = init_model()  # 현재 미사용 (예약). 자연어 요약 도입 시 호출 지점 추가.


def has_azure_backend() -> bool:
    try:
        azure_get_config()
        return True
    except Exception:
        return False


def has_legacy_backend() -> bool:
    try:
        sb = st.secrets["supabase"]
    except Exception:
        return False
    return all(sb.get(k) for k in ("host", "port", "database", "user", "password"))


def get_dataset_config(backend: str) -> dict:
    return AZURE_DATASETS if backend == "azure" else LEGACY_DATASETS


@st.cache_resource
def _connect_db(backend: str):
    if backend == "azure":
        return azure_connect()

    sb = st.secrets["supabase"]
    return psycopg2.connect(
        host=sb["host"],
        port=sb["port"],
        database=sb["database"],
        user=sb["user"],
        password=sb["password"],
        sslmode="require",
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


def init_db(backend: str):
    conn = _connect_db(backend)
    try:
        if conn.closed:
            raise psycopg2.OperationalError("connection closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError):
        try:
            conn.close()
        except Exception:
            pass
        _connect_db.clear()
        return _connect_db(backend)


def read_sql(backend: str, sql: str, params=None) -> pd.DataFrame:
    return pd.read_sql_query(sql, init_db(backend), params=params)


@st.cache_data(ttl=600, show_spinner=False)
def load_zones(backend: str) -> pd.DataFrame:
    if backend == "azure":
        sql = """
            SELECT taz_seq AS "ZONE", sido AS "SIDO", sigu AS "SIGU"
            FROM smr_zones
            ORDER BY taz_seq
        """
    else:
        sql = """
            SELECT zone AS "ZONE", sido AS "SIDO", sigu AS "SIGU"
            FROM zones
            ORDER BY zone
        """
    return read_sql(backend, sql)


def _pivot_year_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    wide = df.pivot_table(
        index=["SIDO", "SIGU", "ZONE"],
        columns="year",
        values="value",
        aggfunc="sum",
    ).reset_index()
    wide.columns = [str(c) for c in wide.columns]
    wide.columns.name = None
    return wide


@st.cache_data(ttl=600, show_spinner=False)
def load_socio_indicator(backend: str, indicator_code: str) -> pd.DataFrame:
    if backend == "legacy":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", s.zone AS "ZONE",
                   s.year, s.value
            FROM socio s
            JOIN zones z USING (zone)
            WHERE s.indicator_code = %s
            ORDER BY s.zone, s.year
        """
        return _pivot_year_table(read_sql(backend, sql, params=(indicator_code,)))

    if indicator_code == "POP_TOT":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code == "POP_YNG":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            WHERE p.age BETWEEN 5 AND 24
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code == "POP_15P":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            WHERE p.age >= 15
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code == "EMP":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", e.taz_seq AS "ZONE",
                   e.year, SUM(e.value) AS value
            FROM smr_socio_emp e
            JOIN smr_zones z ON z.taz_seq = e.taz_seq
            GROUP BY z.sido, z.sigu, e.taz_seq, e.year
            ORDER BY e.taz_seq, e.year
        """
    elif indicator_code == "STU":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", s.taz_seq AS "ZONE",
                   s.year, SUM(s.value) AS value
            FROM smr_socio_stu s
            JOIN smr_zones z ON z.taz_seq = s.taz_seq
            GROUP BY z.sido, z.sigu, s.taz_seq, s.year
            ORDER BY s.taz_seq, s.year
        """
    elif indicator_code == "WORK_TOT":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", w.taz_seq AS "ZONE",
                   w.year, w.value
            FROM smr_socio_work w
            JOIN smr_zones z ON z.taz_seq = w.taz_seq
            WHERE w.indicator = 'TOT '
            ORDER BY w.taz_seq, w.year
        """
    else:
        raise ValueError(f"알 수 없는 사회경제지표: {indicator_code}")

    return _pivot_year_table(read_sql(backend, sql))


AZURE_OD_TABLES = {
    "od_purpose": {
        "physical": "smr_od_purpose",
        "origin": "orgn_seq",
        "dest": "dest_seq",
        "zone_table": "smr_zones",
        "zone_key": "taz_seq",
        "cols": ["home", "work", "scho", "busi", "othe"],
    },
    "od_main_mode": {
        "physical": "smr_od_main_mode",
        "origin": "orgn_seq",
        "dest": "dest_seq",
        "zone_table": "smr_zones",
        "zone_key": "taz_seq",
        "cols": [
            "walk_bike",
            "freight",
            "etc_bus",
            "rail",
            "ktx",
            "auto",
            "taxi",
            "bus",
            "subw",
            "bus_subw",
        ],
    },
    "od_purpose_mode": {
        "physical": "smr_od_purpose_mode",
        "origin": "orgn_seq",
        "dest": "dest_seq",
        "zone_table": "smr_zones",
        "zone_key": "taz_seq",
        "cols": [
            "walk_bike",
            "freight",
            "etc_bus",
            "rail",
            "ktx",
            "auto",
            "taxi",
            "bus",
            "subw",
            "bus_subw",
        ],
        "extra_filter": "purpose",  # purpose=HOME/WORK/SCHO/OTHE
    },
}

LEGACY_OD_TABLES = {
    "od_purpose": {
        "physical": "od_purpose",
        "origin": "orgn",
        "dest": "dest",
        "zone_table": "zones",
        "zone_key": "zone",
        "cols": ["work", "scho", "busi", "home", "othe"],
    },
    "od_main_mode": {
        "physical": "od_main_mode",
        "origin": "orgn",
        "dest": "dest",
        "zone_table": "zones",
        "zone_key": "zone",
        "cols": ["auto", "obus", "subw", "rail", "erai"],
    },
    "od_access_mode": {
        "physical": "od_access_mode",
        "origin": "orgn",
        "dest": "dest",
        "zone_table": "zones",
        "zone_key": "zone",
        "cols": ["att_aant", "att_obus"],
    },
}


@st.cache_data(ttl=600, show_spinner=False)
def load_od(backend: str, table: str, year: int, purpose: str | None = None) -> pd.DataFrame:
    table_cfg = AZURE_OD_TABLES if backend == "azure" else LEGACY_OD_TABLES
    cfg = table_cfg[table]
    cols_sql = ", ".join(f"t.{col}" for col in cfg["cols"])
    sql = f"""
        SELECT zo.sido AS "SIDO", zo.sigu AS "SIGU",
               t.{cfg["origin"]} AS "ORGN", t.{cfg["dest"]} AS "DEST",
               zd.sido AS "DEST_SIDO", zd.sigu AS "DEST_SIGU",
               {cols_sql}
        FROM {cfg["physical"]} t
        LEFT JOIN {cfg["zone_table"]} zo ON t.{cfg["origin"]} = zo.{cfg["zone_key"]}
        LEFT JOIN {cfg["zone_table"]} zd ON t.{cfg["dest"]} = zd.{cfg["zone_key"]}
        WHERE t.year = %s
    """
    params: tuple = (year,)
    extra = cfg.get("extra_filter")
    if extra and purpose:
        sql += f" AND t.{extra} = %s"
        # CHAR(4) padded — 'HOME','WORK','SCHO','OTHE' 모두 4글자라 padding 불필요
        params = (year, purpose)
    elif extra and not purpose:
        raise ValueError(f"{table} 은 purpose 인자가 필요합니다 (HOME/WORK/SCHO/OTHE)")
    df = read_sql(backend, sql, params=params)
    fixed = {"SIDO", "SIGU", "ORGN", "DEST", "DEST_SIDO", "DEST_SIGU"}
    df.columns = [c if c in fixed else c.upper() for c in df.columns]
    return df


def load_sheet_compat(backend: str, file_label: str, tab: str) -> pd.DataFrame:
    if file_label == "사회경제지표":
        if tab == "ZONE":
            return load_zones(backend)
        return load_socio_indicator(backend, tab)
    if file_label == "목적OD":
        return load_od(backend, "od_purpose", int(tab.split("_")[1]))
    if file_label == "주수단OD":
        return load_od(backend, "od_main_mode", int(tab.split("_")[1]))
    if file_label == "목적별주수단OD":
        # tab format: PURMOD_<HOME|WORK|SCHO|OTHE>_<year>
        parts = tab.split("_")
        if len(parts) != 3 or parts[0] != "PURMOD":
            raise ValueError(f"잘못된 목적별주수단OD tab: {tab}")
        return load_od(backend, "od_purpose_mode", int(parts[2]), purpose=parts[1])
    if file_label == "접근수단OD":
        return load_od(backend, "od_access_mode", 2023)
    raise ValueError(f"알 수 없는 시트 매핑: {file_label}/{tab}")


@st.cache_data(ttl=3600, show_spinner=False)
def get_all_regions(backend: str) -> tuple[list[str], list[str]]:
    if backend == "azure":
        sql = "SELECT DISTINCT sido, sigu FROM smr_zones"
    else:
        sql = "SELECT DISTINCT sido, sigu FROM zones"
    df = read_sql(backend, sql)
    sidos = sorted(df["sido"].dropna().unique().tolist(), key=len, reverse=True)
    sigus = sorted(df["sigu"].dropna().unique().tolist(), key=len, reverse=True)
    return sidos, sigus


@st.cache_data(ttl=600, show_spinner=False)
def get_sido_options(backend: str) -> list[str]:
    if backend == "azure":
        sql = "SELECT DISTINCT sido FROM smr_zones ORDER BY sido"
    else:
        sql = "SELECT DISTINCT sido FROM zones ORDER BY sido"
    df = read_sql(backend, sql)
    return ["전체"] + df["sido"].dropna().tolist()


@st.cache_data(ttl=600, show_spinner=False)
def get_sigu_list(backend: str, sido: str) -> list[str]:
    try:
        if backend == "azure":
            base = "SELECT DISTINCT sigu FROM smr_zones"
        else:
            base = "SELECT DISTINCT sigu FROM zones"

        if sido == "전체":
            df = read_sql(backend, f"{base} ORDER BY sigu")
        else:
            df = read_sql(backend, f"{base} WHERE sido LIKE %s ORDER BY sigu", params=(f"%{sido}%",))
        return ["전체"] + df["sigu"].dropna().tolist()
    except Exception:
        return ["전체"]


def extract_region_from_query(backend: str, query: str) -> tuple[str | None, str | None]:
    if not query:
        return None, None
    sidos, sigus = get_all_regions(backend)
    matched_sido = next((s for s in sidos if s in query), None)
    matched_sigu = next((s for s in sigus if s in query), None)
    return matched_sido, matched_sigu


def preprocess(
    df: pd.DataFrame,
    *,
    backend: str,
    query: str,
    sido_sel: str,
    sigu_sel: str,
) -> pd.DataFrame:
    df = df.copy()

    sido_eff, sigu_eff = sido_sel, sigu_sel
    if sido_eff == "전체" or sigu_eff == "전체":
        auto_sido, auto_sigu = extract_region_from_query(backend, query)
        if sido_eff == "전체" and auto_sido:
            sido_eff = auto_sido
        if sigu_eff == "전체" and auto_sigu:
            sigu_eff = auto_sigu

    if "SIDO" in df.columns and sido_eff != "전체":
        df = df[df["SIDO"].astype(str).str.contains(sido_eff, na=False)]
    if "SIGU" in df.columns and sigu_eff != "전체":
        df = df[df["SIGU"].astype(str).str.contains(sigu_eff, na=False)]

    sort_col = next((c for c in ["ZONE", "ORGN"] if c in df.columns), None)
    if sort_col:
        df = df.sort_values(sort_col)

    df.rename(columns={c: COL_KR.get(c, c) for c in df.columns}, inplace=True)
    return df


def interpolate_years(df: pd.DataFrame, target_years: list[int]) -> tuple[pd.DataFrame, list[int]]:
    if not any(YEAR_COLUMN_RE.match(str(col)) for col in df.columns):
        return df, []

    df = df.copy()
    interp_years = []
    for y in target_years:
        col_name = f"{y}년"
        if col_name in df.columns or y in DIST_YEARS:
            continue
        lower = max([d for d in DIST_YEARS if d <= y], default=None)
        upper = min([d for d in DIST_YEARS if d >= y], default=None)
        if lower and upper and lower != upper:
            lower_col = f"{lower}년"
            upper_col = f"{upper}년"
            if lower_col in df.columns and upper_col in df.columns:
                ratio = (y - lower) / (upper - lower)
                df[col_name] = (
                    pd.to_numeric(df[lower_col], errors="coerce")
                    + ratio
                    * (
                        pd.to_numeric(df[upper_col], errors="coerce")
                        - pd.to_numeric(df[lower_col], errors="coerce")
                    )
                ).round(1)
                interp_years.append(y)
    return df, interp_years


def load_integrated(
    backend: str,
    file_label: str,
    tab: str,
    target_years: list[int],
    query: str,
    sido_sel: str,
    sigu_sel: str,
) -> tuple[pd.DataFrame, list[int]]:
    df = load_sheet_compat(backend, file_label, tab)
    df = preprocess(df, backend=backend, query=query, sido_sel=sido_sel, sigu_sel=sigu_sel)
    return interpolate_years(df, target_years)


def extract_years_from_query(query: str) -> list[int]:
    years = set(int(y) for y in re.findall(r"\b(20\d{2})\b", query))
    range_match = re.search(r"(20\d{2})\s*[~∼\-–]+\s*(20\d{2})", query)
    if range_match:
        a, b = int(range_match.group(1)), int(range_match.group(2))
        lo, hi = min(a, b), max(a, b)
        step_match = re.search(r"(\d+)\s*년\s*단위", query)
        if step_match:
            step = int(step_match.group(1))
            years.update(range(lo, hi + 1, step))
        else:
            years.update(y for y in DIST_YEARS if lo <= y <= hi)
    return sorted(years)


def detect_aggregation(query: str) -> str | None:
    if any(k in query for k in ["시군구별", "시군구 별"]):
        return "시군구"
    if any(k in query for k in ["시도별", "시도 별", "지역별", "지역 별", "지역 분포", "지역분포"]):
        return "시도"
    return None


def aggregate_by_region(df: pd.DataFrame, level: str) -> pd.DataFrame:
    if level not in df.columns:
        return df
    numeric_cols = [c for c in df.select_dtypes(include="number").columns if c not in {"존번호", "발생존", "도착존"}]
    if not numeric_cols:
        return df
    return df.groupby(level, dropna=False)[numeric_cols].sum().reset_index()


def get_user_years(year_inputs: list[str]) -> list[int]:
    result = []
    for raw in year_inputs:
        value = raw.strip() if raw else ""
        if value.isdigit():
            result.append(int(value))
    return sorted(set(result)) if result else DIST_YEARS


def _append_combo(combos: list[tuple[str, str]], item: tuple[str, str]) -> None:
    if item not in combos:
        combos.append(item)


def _od_tab_years(target_years: list[int]) -> list[int]:
    years = [y for y in target_years if y in DIST_YEARS]
    return years or [2023]


def auto_route(query: str, backend: str, target_years: list[int]) -> list[tuple[str, str]]:
    config = get_dataset_config(backend)
    combos: list[tuple[str, str]] = []
    lowered = query.lower()

    if any(token in query for token in ["인구", "인구수"]):
        if any(token in query for token in ["5-24", "5~24", "청년", "유년"]):
            _append_combo(combos, ("사회경제지표", "POP_YNG"))
        elif any(token in query for token in ["15세이상", "15세 이상"]):
            _append_combo(combos, ("사회경제지표", "POP_15P"))
        else:
            _append_combo(combos, ("사회경제지표", "POP_TOT"))

    if "취업자" in query:
        _append_combo(combos, ("사회경제지표", "EMP"))
    if any(token in query for token in ["학생", "재학생"]):
        _append_combo(combos, ("사회경제지표", "STU"))
    if "종사자" in query:
        _append_combo(combos, ("사회경제지표", "WORK_TOT"))

    od_years = _od_tab_years(target_years)
    if any(token in query for token in ["통행", "od", "출근", "등교", "귀가", "업무", "기타"]):
        if "목적OD" in config:
            for year in od_years:
                _append_combo(combos, ("목적OD", f"PUR_{year}"))

    mode_tokens = [
        "도보",
        "자전거",
        "화물",
        "버스",
        "지하철",
        "철도",
        "ktx",
        "고속철도",
        "택시",
        "승용차",
        "자동차",
        "주수단",
    ]
    mode_present = any(token in lowered for token in mode_tokens)
    if mode_present:
        if "주수단OD" in config:
            for year in od_years:
                _append_combo(combos, ("주수단OD", f"MOD_{year}"))

    # 목적별주수단OD: (1) 명시 키워드 → 4목적 모두, (2) 수단 + 목적 키워드 동시 → 매칭 목적만
    explicit_pm = any(
        token in query for token in ["목적별주수단", "목적별 주수단", "목적별 수단", "수단별 목적"]
    )
    matched_purposes = [code for kr, code in PURPOSE_KR_TO_CODE.items() if kr in query]
    if "목적별주수단OD" in config:
        if explicit_pm:
            for year in od_years:
                for code in ["HOME", "WORK", "SCHO", "OTHE"]:
                    _append_combo(combos, ("목적별주수단OD", f"PURMOD_{code}_{year}"))
        elif mode_present and matched_purposes:
            for year in od_years:
                for code in matched_purposes:
                    _append_combo(combos, ("목적별주수단OD", f"PURMOD_{code}_{year}"))

    if backend == "legacy" and any(token in query for token in ["접근", "환승"]):
        _append_combo(combos, ("접근수단OD", "ATTMOD_2023"))

    if not combos:
        _append_combo(combos, ("사회경제지표", "POP_TOT"))

    return combos[:8]


def key_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in KEY_COLUMNS_KR if col in df.columns]


def pick_value_columns(file_label: str, df: pd.DataFrame, query: str, target_years: list[int]) -> list[str]:
    if file_label == "사회경제지표":
        preferred = [f"{year}년" for year in target_years if f"{year}년" in df.columns]
        if preferred:
            return preferred
        return [col for col in df.columns if YEAR_COLUMN_RE.match(str(col))]

    value_cols = [col for col in df.columns if col not in key_columns(df)]
    hints = OD_QUERY_HINTS.get(file_label, {})
    selected = []
    lowered = query.lower()
    for token, column_name in hints.items():
        token_lower = token.lower()
        if token in query or token_lower in lowered:
            if column_name in value_cols and column_name not in selected:
                selected.append(column_name)
    return selected or value_cols


def parse_tab_year(tab: str) -> int | None:
    match = re.search(r"_(20\d{2})$", tab)
    return int(match.group(1)) if match else None


def build_display_part(dataset: dict, query: str, target_years: list[int]) -> pd.DataFrame:
    df = dataset["df_for_display"].copy()
    keys = key_columns(df)
    values = pick_value_columns(dataset["file"], df, query, target_years)
    keep = keys + values
    part = df[keep].copy()

    if dataset["file"] == "사회경제지표":
        rename_map = {col: f"{dataset['tab_kr']} {col}" for col in values}
    else:
        year = parse_tab_year(dataset["tab"])
        suffix = f" ({year}년)" if year else ""
        rename_map = {col: f"{col}{suffix}" for col in values}

    part.rename(columns=rename_map, inplace=True)
    return part


def merge_parts(parts: list[pd.DataFrame]) -> pd.DataFrame:
    merged = parts[0]
    for part in parts[1:]:
        join_keys = [col for col in KEY_COLUMNS_KR if col in merged.columns and col in part.columns]
        if join_keys:
            merged = merged.merge(part, on=join_keys, how="outer")
        else:
            merged = pd.concat([merged, part], ignore_index=True, sort=False)
    return merged


def sort_result_frame(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["존번호", "발생존", "도착존", "시군구", "시도"]:
        if col in df.columns:
            return df.sort_values(col)
    return df


def compact_large_result(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    if len(df) <= 2000:
        return df, None

    numeric_cols = [col for col in df.select_dtypes(include="number").columns if col not in {"존번호", "발생존", "도착존"}]
    if "시도" in df.columns and "시군구" in df.columns and numeric_cols:
        grouped = df.groupby(["시도", "시군구"], dropna=False)[numeric_cols].sum().reset_index()
        return grouped, f"원본 행 수가 많아 시도·시군구별 합계 {len(grouped):,}행으로 축약했습니다."
    if "시군구" in df.columns and numeric_cols:
        return (
            df.groupby("시군구", dropna=False)[numeric_cols].sum().reset_index(),
            f"원본 행 수가 많아 시군구별 합계 {df['시군구'].nunique()}행으로 축약했습니다.",
        )
    if "시도" in df.columns and numeric_cols:
        return (
            df.groupby("시도", dropna=False)[numeric_cols].sum().reset_index(),
            f"원본 행 수가 많아 시도별 합계 {df['시도'].nunique()}행으로 축약했습니다.",
        )
    return df.head(2000), "원본 행 수가 많아 상위 2,000행만 표시합니다."


def build_result_tables(datasets: list[dict], query: str, target_years: list[int]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for dataset in datasets:
        grouped.setdefault(dataset["file"], []).append(dataset)

    tables = []
    for file_label, items in grouped.items():
        parts = [build_display_part(item, query, target_years) for item in items]
        merged = sort_result_frame(merge_parts(parts))
        compacted, note = compact_large_result(merged)
        tables.append(
            {
                "title": file_label,
                "df": compacted,
                "note": note,
                "unit": UNITS.get(file_label, ""),
            }
        )
    return tables


def build_summary(
    *,
    backend: str,
    tables: list[dict],
    combos: list[tuple[str, str]],
    interp_years: list[int],
    agg_level: str | None,
    auto_sido: str | None,
    auto_sigu: str | None,
) -> str:
    lines = [f"- 백엔드: {BACKEND_LABELS[backend]}"]
    lines.append(f"- 결과 표: {len(tables)}개, 선택 데이터셋: {len(combos)}개")

    if auto_sigu:
        lines.append(f"- 자동 인식 지역: {auto_sigu}")
    elif auto_sido:
        lines.append(f"- 자동 인식 지역: {auto_sido}")

    if agg_level:
        lines.append(f"- 집계 수준: {agg_level}별 합계")
    if interp_years:
        lines.append(f"- 보간 연도: {', '.join(str(year) for year in interp_years)}")

    units = sorted({table["unit"] for table in tables if table["unit"]})
    if units:
        lines.append(f"- 단위: {', '.join(units)}")

    return "\n".join(lines)


def render_tables(tables: list[dict], message_idx: int) -> None:
    for table_idx, table in enumerate(tables):
        title = table.get("title")
        unit = table.get("unit")
        note = table.get("note")
        if title:
            label = f"**{title}**"
            if unit:
                label = f"{label} · 단위 `{unit}`"
            st.markdown(label)
        if note:
            st.caption(note)

        df = table["df"]
        df_show = df.T if st.session_state.transpose else df
        st.dataframe(df_show, use_container_width=True)
        csv_bytes = ("\ufeff" + df.to_csv(index=False)).encode("utf-8")
        st.download_button(
            "CSV 다운로드",
            data=csv_bytes,
            file_name=f"ktdb_result_{message_idx}_{table_idx}.csv",
            mime="text/csv; charset=utf-8",
            key=f"dl_{message_idx}_{table_idx}",
        )


for key, default in {
    "messages": [],
    "sel_file": None,
    "sel_tab": None,
    "transpose": False,
    "manual_mode": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


available_backends = []
if has_azure_backend():
    available_backends.append("azure")
if has_legacy_backend():
    available_backends.append("legacy")

if not available_backends:
    st.error("사용 가능한 DB 백엔드가 없습니다. `[azure]` 또는 `[supabase]` 설정을 확인하세요.")
    st.stop()


with st.sidebar:
    st.title("분석 조건")

    backend = st.radio(
        "데이터 백엔드",
        options=available_backends,
        index=0,
        format_func=lambda key: BACKEND_LABELS[key],
    )
    if backend == "legacy":
        st.caption("Legacy Supabase fallback 경로입니다.")
    elif has_legacy_backend():
        st.caption("Azure smr_*를 기본 경로로 사용 중입니다.")

    st.subheader("분석 대상 지역")
    sido_options = get_sido_options(backend)
    sido_sel = st.selectbox("시도", sido_options)
    sigu_sel = st.selectbox("시군구", get_sigu_list(backend, sido_sel))

    st.divider()
    st.subheader("분석 연도")
    st.caption("배포 연도 외 입력은 사회경제지표에만 선형보간을 적용합니다.")
    year_base = st.text_input("기준연도", placeholder="예: 2023")
    col1, col2 = st.columns(2)
    with col1:
        year_mid1 = st.text_input("중간목표①", placeholder="예: 2030")
    with col2:
        year_mid2 = st.text_input("중간목표②", placeholder="예: 2040")
    year_mid3 = st.text_input("중간목표③", placeholder="예: 2045")
    year_final = st.text_input("최종목표연도", placeholder="예: 2050")

    st.divider()
    st.subheader("데이터셋 선택")
    manual_mode = st.toggle(
        "직접 선택",
        value=st.session_state.manual_mode,
        help="OFF면 규칙 기반 자동 라우팅을 사용합니다.",
    )
    st.session_state.manual_mode = manual_mode

    dataset_config = get_dataset_config(backend)
    if manual_mode:
        file_options = list(dataset_config.keys())
        file_labels = ["— 파일을 선택하세요 —"] + file_options
        current_file_idx = file_labels.index(st.session_state.sel_file) if st.session_state.sel_file in file_labels else 0
        selected_file = st.selectbox("파일", file_labels, index=current_file_idx)

        if selected_file == "— 파일을 선택하세요 —":
            st.session_state.sel_file = None
            st.session_state.sel_tab = None
        else:
            st.session_state.sel_file = selected_file
            tab_options = list(dataset_config[selected_file]["tabs"].keys())
            tab_labels = ["— 시트를 선택하세요 —"] + [
                f"{tab} — {dataset_config[selected_file]['tabs'][tab]}" for tab in tab_options
            ]
            current_tab_display = (
                f"{st.session_state.sel_tab} — {dataset_config[selected_file]['tabs'].get(st.session_state.sel_tab, '')}"
                if st.session_state.sel_tab in tab_options
                else "— 시트를 선택하세요 —"
            )
            current_tab_idx = tab_labels.index(current_tab_display) if current_tab_display in tab_labels else 0
            selected_tab_label = st.selectbox("시트(탭)", tab_labels, index=current_tab_idx)
            if selected_tab_label == "— 시트를 선택하세요 —":
                st.session_state.sel_tab = None
            else:
                st.session_state.sel_tab = tab_options[tab_labels.index(selected_tab_label) - 1]
                st.caption(f"고정: `{selected_file}` > `{st.session_state.sel_tab}`")
    else:
        st.caption("질문 키워드와 연도를 기준으로 자동 라우팅합니다.")
        if model is None:
            st.caption("Gemini 키가 없어도 표 생성은 계속됩니다.")

    if backend == "legacy":
        st.divider()
        with st.expander("데이터 동기화 (시트→Legacy DB)"):
            st.caption("Legacy Supabase만 지원합니다. Azure `sync_smr.py` 경로와는 분리됩니다.")
            if st.button("동기화 실행"):
                from sync_sheets_to_db import sync_all

                urls = {
                    "socio": st.secrets["SHEET_URL_SOCIO"],
                    "obj_od": st.secrets["SHEET_URL_OBJ_OD"],
                    "main_od": st.secrets["SHEET_URL_MAIN_OD"],
                    "acc_od": st.secrets["SHEET_URL_ACC_OD"],
                }
                with st.spinner("동기화 중..."):
                    report = sync_all(init_db("legacy"), urls)
                    st.cache_data.clear()
                for item in report:
                    if item.get("rows", 0) > 0:
                        st.success(f"{item['table']}: {item['rows']:,}행 ({item['duration_s']}s)")
                    else:
                        st.error(f"{item['table']}: {item.get('error', '실패')}")

    if st.button("대화 초기화"):
        st.session_state.messages = []
        st.rerun()


st.title("KTDB 통합 분석 에이전트")

for idx, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if "tables" in message:
            render_tables(message["tables"], idx)

if any("tables" in message for message in st.session_state.messages):
    st.session_state.transpose = st.toggle("행·열 전환", value=st.session_state.transpose)


if user_input := st.chat_input("질문을 입력하세요 — 예: 경기도 2030년 인구수와 종사자수 비교"):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        chat_years = extract_years_from_query(user_input)
        target_years = sorted(
            set(
                get_user_years([year_base, year_mid1, year_mid2, year_mid3, year_final])
                + chat_years
            )
        )

        if manual_mode and st.session_state.sel_file and st.session_state.sel_tab:
            combos = [(st.session_state.sel_file, st.session_state.sel_tab)]
        elif manual_mode:
            st.warning("직접 선택 모드입니다. 사이드바에서 파일과 시트를 선택해 주세요.")
            st.stop()
        else:
            combos = auto_route(user_input, backend, target_years)

        combo_labels = [
            f"**{file_label}** > **{dataset_config[file_label]['tabs'][tab]}**"
            for file_label, tab in combos
        ]
        st.caption(f"선택 데이터셋: {', '.join(combo_labels)}")

        datasets = []
        interp_years = set()
        agg_level = detect_aggregation(user_input)
        auto_sido, auto_sigu = extract_region_from_query(backend, user_input)

        with st.spinner(f"데이터 로딩 중... ({len(combos)}개)"):
            try:
                for file_label, tab in combos:
                    df, interp = load_integrated(
                        backend,
                        file_label,
                        tab,
                        target_years,
                        user_input,
                        sido_sel,
                        sigu_sel,
                    )
                    df_for_display = aggregate_by_region(df, agg_level) if agg_level else df
                    datasets.append(
                        {
                            "file": file_label,
                            "tab": tab,
                            "tab_kr": dataset_config[file_label]["tabs"][tab],
                            "df": df,
                            "df_for_display": df_for_display,
                        }
                    )
                    interp_years.update(interp)
            except Exception as exc:
                st.error(f"데이터 로드 실패: {exc}")
                st.stop()

        tables = build_result_tables(datasets, user_input, target_years)
        summary = build_summary(
            backend=backend,
            tables=tables,
            combos=combos,
            interp_years=sorted(interp_years),
            agg_level=agg_level,
            auto_sido=auto_sido,
            auto_sigu=auto_sigu,
        )
        st.markdown(summary)
        render_tables(tables, len(st.session_state.messages))
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": summary,
                "tables": tables,
            }
        )
