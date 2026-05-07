import io
import re

import google.generativeai as genai
import pandas as pd
import psycopg2
import streamlit as st

from db_env import connect as azure_connect
from db_env import get_config as azure_get_config


st.set_page_config(page_title="KTDB 통합 분석 에이전트", layout="wide")

# session_state 초기화는 set_page_config 직후가 가장 안전.
# 자동 리로드 시점에 이 값이 사라지면 후속 .messages 접근에서 AttributeError 발생.
for _k, _v in {
    "messages": [],
    "sel_file": None,
    "sel_tab": None,
    "transpose": False,
    "manual_mode": False,
}.items():
    st.session_state.setdefault(_k, _v)

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
            "POP_0_14": "0-14세 인구수",
            "POP_15_64": "생산가능인구(15-64세)",
            "POP_65P": "65세이상 고령인구",
            "POP_MALE": "남성 인구",
            "POP_FEMALE": "여성 인구",
            "EMP": "취업자수",
            "EMP_MALE": "남성 취업자",
            "EMP_FEMALE": "여성 취업자",
            "STU": "학생수(전체)",
            "STU_ELEM": "초등학생",
            "STU_MID": "중학생",
            "STU_HIGH": "고등학생",
            "STU_SPEC": "특수학교 학생",
            "STU_UNIV": "대학생",
            "WORK_TOT": "총 종사자",
            "WORK_3RD": "3차산업 종사자",
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
    if not api_key or not api_key.strip():
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
    elif indicator_code == "WORK_3RD":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", w.taz_seq AS "ZONE",
                   w.year, w.value
            FROM smr_socio_work w
            JOIN smr_zones z ON z.taz_seq = w.taz_seq
            WHERE w.indicator = '3RD '
            ORDER BY w.taz_seq, w.year
        """
    elif indicator_code == "POP_0_14":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            WHERE p.age BETWEEN 0 AND 14
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code == "POP_15_64":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            WHERE p.age BETWEEN 15 AND 64
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code == "POP_65P":
        sql = """
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            WHERE p.age >= 65
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code in ("POP_MALE", "POP_FEMALE"):
        gender = "M" if indicator_code == "POP_MALE" else "F"
        sql = f"""
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", p.taz_seq AS "ZONE",
                   p.year, SUM(p.value) AS value
            FROM smr_socio_pop p
            JOIN smr_zones z ON z.taz_seq = p.taz_seq
            WHERE p.gender = '{gender}'
            GROUP BY z.sido, z.sigu, p.taz_seq, p.year
            ORDER BY p.taz_seq, p.year
        """
    elif indicator_code in ("EMP_MALE", "EMP_FEMALE"):
        gender = "M" if indicator_code == "EMP_MALE" else "F"
        sql = f"""
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", e.taz_seq AS "ZONE",
                   e.year, SUM(e.value) AS value
            FROM smr_socio_emp e
            JOIN smr_zones z ON z.taz_seq = e.taz_seq
            WHERE e.gender = '{gender}'
            GROUP BY z.sido, z.sigu, e.taz_seq, e.year
            ORDER BY e.taz_seq, e.year
        """
    elif indicator_code in ("STU_ELEM", "STU_MID", "STU_HIGH", "STU_SPEC", "STU_UNIV"):
        # CHAR(4) padded
        level_map = {
            "STU_ELEM": "ELEM",
            "STU_MID": "MID ",
            "STU_HIGH": "HIGH",
            "STU_SPEC": "SPEC",
            "STU_UNIV": "UNIV",
        }
        level = level_map[indicator_code]
        sql = f"""
            SELECT z.sido AS "SIDO", z.sigu AS "SIGU", s.taz_seq AS "ZONE",
                   s.year, s.value
            FROM smr_socio_stu s
            JOIN smr_zones z ON z.taz_seq = s.taz_seq
            WHERE s.level = '{level}'
            ORDER BY s.taz_seq, s.year
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
def load_od(
    backend: str,
    table: str,
    year: int,
    purpose: str | None = None,
    origin_sido: str | None = None,
    origin_sigu: str | None = None,
    dest_sido: str | None = None,
    dest_sigu: str | None = None,
    origin_scope: str | None = None,
    dest_scope: str | None = None,
    pre_aggregate_level: str | None = None,
) -> pd.DataFrame:
    """OD 데이터 로드. 필터·집계는 SQL 단계에 푸시다운하여 1.5M → 수만/수십 행으로 축소.

    필터 종류:
      - origin/dest sido/sigu: LIKE '%경기도%' / '%수원시%'
      - origin/dest scope: 'inner'(서울·인천·경기) / 'outer' (그 외)

    사전 집계 (도착지/발생존 정보 필요 없을 때):
      - pre_aggregate_level='origin_sido'  → GROUP BY zo.sido (10~17행)
      - pre_aggregate_level='origin_sigu'  → GROUP BY zo.sido, zo.sigu (수십 행)
      - pre_aggregate_level='dest_sido'    → GROUP BY zd.sido
      - pre_aggregate_level='dest_sigu'    → GROUP BY zd.sido, zd.sigu
    """
    table_cfg = AZURE_OD_TABLES if backend == "azure" else LEGACY_OD_TABLES
    cfg = table_cfg[table]

    where = ["t.year = %s"]
    params: list = [year]

    extra = cfg.get("extra_filter")
    if extra and purpose:
        where.append(f"t.{extra} = %s")
        params.append(purpose)
    elif extra and not purpose:
        raise ValueError(f"{table} 은 purpose 인자가 필요합니다 (HOME/WORK/SCHO/OTHE)")

    if origin_sido and origin_sido != "전체":
        where.append("zo.sido LIKE %s")
        params.append(f"%{origin_sido}%")
    if origin_sigu and origin_sigu != "전체":
        where.append("zo.sigu LIKE %s")
        params.append(f"%{origin_sigu}%")
    if dest_sido and dest_sido != "전체":
        where.append("zd.sido LIKE %s")
        params.append(f"%{dest_sido}%")
    if dest_sigu and dest_sigu != "전체":
        where.append("zd.sigu LIKE %s")
        params.append(f"%{dest_sigu}%")

    inner_sidos_sql = "(" + ", ".join(f"'{s}'" for s in INNER_SIDOS) + ")"
    if origin_scope == "inner":
        where.append(f"zo.sido IN {inner_sidos_sql}")
    elif origin_scope == "outer":
        where.append(f"zo.sido NOT IN {inner_sidos_sql}")
    if dest_scope == "inner":
        where.append(f"zd.sido IN {inner_sidos_sql}")
    elif dest_scope == "outer":
        where.append(f"zd.sido NOT IN {inner_sidos_sql}")

    where_sql = " AND ".join(where)

    if pre_aggregate_level:
        sum_cols = ", ".join(f"SUM(t.{col}) AS {col}" for col in cfg["cols"])
        if pre_aggregate_level == "origin_sido":
            select_cols = 'zo.sido AS "SIDO"'
            group_cols = "zo.sido"
        elif pre_aggregate_level == "origin_sigu":
            select_cols = 'zo.sido AS "SIDO", zo.sigu AS "SIGU"'
            group_cols = "zo.sido, zo.sigu"
        elif pre_aggregate_level == "dest_sido":
            select_cols = 'zd.sido AS "DEST_SIDO"'
            group_cols = "zd.sido"
        elif pre_aggregate_level == "dest_sigu":
            select_cols = 'zd.sido AS "DEST_SIDO", zd.sigu AS "DEST_SIGU"'
            group_cols = "zd.sido, zd.sigu"
        else:
            raise ValueError(f"unknown pre_aggregate_level: {pre_aggregate_level}")
        sql = f"""
            SELECT {select_cols}, {sum_cols}
            FROM {cfg["physical"]} t
            LEFT JOIN {cfg["zone_table"]} zo ON t.{cfg["origin"]} = zo.{cfg["zone_key"]}
            LEFT JOIN {cfg["zone_table"]} zd ON t.{cfg["dest"]} = zd.{cfg["zone_key"]}
            WHERE {where_sql}
            GROUP BY {group_cols}
        """
    else:
        cols_sql = ", ".join(f"t.{col}" for col in cfg["cols"])
        sql = f"""
            SELECT zo.sido AS "SIDO", zo.sigu AS "SIGU",
                   t.{cfg["origin"]} AS "ORGN", t.{cfg["dest"]} AS "DEST",
                   zd.sido AS "DEST_SIDO", zd.sigu AS "DEST_SIGU",
                   {cols_sql}
            FROM {cfg["physical"]} t
            LEFT JOIN {cfg["zone_table"]} zo ON t.{cfg["origin"]} = zo.{cfg["zone_key"]}
            LEFT JOIN {cfg["zone_table"]} zd ON t.{cfg["dest"]} = zd.{cfg["zone_key"]}
            WHERE {where_sql}
        """
    df = read_sql(backend, sql, params=tuple(params))
    fixed = {"SIDO", "SIGU", "ORGN", "DEST", "DEST_SIDO", "DEST_SIGU"}
    df.columns = [c if c in fixed else c.upper() for c in df.columns]
    return df


def load_sheet_compat(
    backend: str,
    file_label: str,
    tab: str,
    *,
    origin_sido: str | None = None,
    origin_sigu: str | None = None,
    dest_sido: str | None = None,
    dest_sigu: str | None = None,
    origin_scope: str | None = None,
    dest_scope: str | None = None,
    pre_aggregate_level: str | None = None,
) -> pd.DataFrame:
    if file_label == "사회경제지표":
        if tab == "ZONE":
            return load_zones(backend)
        return load_socio_indicator(backend, tab)
    od_kwargs = dict(
        origin_sido=origin_sido,
        origin_sigu=origin_sigu,
        dest_sido=dest_sido,
        dest_sigu=dest_sigu,
        origin_scope=origin_scope,
        dest_scope=dest_scope,
        pre_aggregate_level=pre_aggregate_level,
    )
    if file_label == "목적OD":
        return load_od(backend, "od_purpose", int(tab.split("_")[1]), **od_kwargs)
    if file_label == "주수단OD":
        return load_od(backend, "od_main_mode", int(tab.split("_")[1]), **od_kwargs)
    if file_label == "목적별주수단OD":
        parts = tab.split("_")
        if len(parts) != 3 or parts[0] != "PURMOD":
            raise ValueError(f"잘못된 목적별주수단OD tab: {tab}")
        return load_od(backend, "od_purpose_mode", int(parts[2]), purpose=parts[1], **od_kwargs)
    if file_label == "접근수단OD":
        return load_od(backend, "od_access_mode", 2023, **od_kwargs)
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


# 사용자 표현 → DB의 실제 sido 풀네임.
# KTDB는 옛 행정명(강원도/전라북도)을 그대로 사용 — 새 명칭(강원특별자치도/전북특별자치도)
# 입력에 대해서도 옛 명칭으로 매핑.
SIDO_ALIASES = {
    "서울": "서울특별시",
    "부산": "부산광역시",
    "대구": "대구광역시",
    "인천": "인천광역시",
    "광주": "광주광역시",
    "대전": "대전광역시",
    "울산": "울산광역시",
    "세종": "세종특별자치시",
    "경기": "경기도",
    "강원": "강원도",
    "강원특별자치도": "강원도",
    "충북": "충청북도",
    "충남": "충청남도",
    "전북": "전라북도",
    "전북특별자치도": "전라북도",
    "전남": "전라남도",
    "경북": "경상북도",
    "경남": "경상남도",
    "제주": "제주특별자치도",
    "제주도": "제주특별자치도",
}


INNER_SIDOS = ["서울특별시", "인천광역시", "경기도"]


def detect_region_scope(query: str) -> tuple[str | None, str | None]:
    """origin/dest 권역 범위 추출 (수도권 권역내부=inner / 권역외부=outer).

    Returns: (origin_scope, dest_scope), each in {None, 'inner', 'outer'}.
    """
    if not query:
        return None, None
    map_ = {"권역내부": "inner", "권역외부": "outer"}

    # 양단 패턴: "권역X에서 권역Y로" / "권역X → 권역Y" / "권역X 유입/유출 권역Y"
    m = re.search(r"(권역내부|권역외부)\s*(?:에서|→|->|=>)\s*(?:[^권]*?)(권역내부|권역외부)", query)
    if m:
        return map_[m.group(1)], map_[m.group(2)]
    # 유입/유출 명시
    if "유입" in query and "권역내부" in query:
        return "outer", "inner"
    if "유출" in query and "권역내부" in query:
        return "inner", "outer"

    # 단일 표현 — origin 기본
    if "권역외부" in query or "수도권 외" in query or "수도권외" in query:
        return "outer", None
    if "권역내부" in query:
        return "inner", None
    # "수도권" 단독 (외부/외 키워드 없을 때) → 권역내부로 간주
    if "수도권" in query:
        return "inner", None
    return None, None


def extract_od_pair_from_query(
    backend: str, query: str
) -> tuple[tuple[str | None, str | None], tuple[str | None, str | None]]:
    """OD 양단 질의 파싱. 방향 마커가 있으면 origin/dest 분리.

    인식 패턴:
      - "A → B" / "A->B" / "A ⇒ B"  → origin=A, dest=B
      - "A ↔ B" / "A ⇄ B"           → origin=A, dest=B (양방향이지만 단방향으로 처리)
      - "A에서 B로/까지/에"            → origin=A, dest=B
    방향 마커 없으면 단일 region을 origin으로 반환.
    """
    if not query:
        return (None, None), (None, None)

    arrow = re.search(r"(.+?)\s*(?:↔|⇄|⇒|→|->|=>)\s*(.+)", query)
    if arrow:
        a_part, b_part = arrow.group(1), arrow.group(2)
        return (
            extract_region_from_query(backend, a_part),
            extract_region_from_query(backend, b_part),
        )

    eseo = re.search(r"(.+?)\s*에서\s*(.+?)\s*(?:으로|로|까지|에)\b", query)
    if eseo:
        a_part, b_part = eseo.group(1), eseo.group(2)
        a_region = extract_region_from_query(backend, a_part)
        b_region = extract_region_from_query(backend, b_part)
        # B 측에 region이 잡혔을 때만 OD 양단 인정 ("강남구에서 출근통행"은 단일)
        if any(b_region):
            return a_region, b_region

    return extract_region_from_query(backend, query), (None, None)


def extract_region_from_query(backend: str, query: str) -> tuple[str | None, str | None]:
    if not query:
        return None, None
    sidos, sigus = get_all_regions(backend)

    # 1차 시도: 풀네임 (예: query "경기도" → DB "경기도")
    matched_sido = next((s for s in sidos if s in query), None)
    # 2차 시도 fallback: 약칭 → 풀네임 (예: query "서울" → DB "서울특별시")
    if not matched_sido:
        for alias, full in SIDO_ALIASES.items():
            if alias in query and full in sidos:
                matched_sido = full
                break

    # 1차 시군구: 풀네임 매칭 (예: query "수원시 영통구" → DB "수원시 영통구")
    matched_sigu = next((s for s in sigus if s in query), None)
    # 2차 시군구 fallback: 시 단위 매칭 — DB가 "수원시 권선구" 등 4개 구로 저장된 경우
    # query에 "수원시"만 있어도 첫 단어(시) 매칭으로 부분일치 키워드를 돌려준다.
    # preprocess()의 str.contains가 이 키워드로 4개 구를 모두 흡수.
    if not matched_sigu:
        first_words = sorted({s.split()[0] for s in sigus if s and " " in s})
        matched_sigu = next((w for w in first_words if w in query), None)
    return matched_sido, matched_sigu


def preprocess(
    df: pd.DataFrame,
    *,
    backend: str,
    query: str,
    sido_sel: str,
    sigu_sel: str,
    dest_sido_sel: str = "전체",
    dest_sigu_sel: str = "전체",
    origin_scope: str | None = None,
    dest_scope: str | None = None,
) -> pd.DataFrame:
    df = df.copy()

    sido_eff, sigu_eff = sido_sel, sigu_sel
    # 자동 region fallback은 OD pair 미인식 시에만 (caller가 이미 처리하면 그대로).
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
    if "DEST_SIDO" in df.columns and dest_sido_sel != "전체":
        df = df[df["DEST_SIDO"].astype(str).str.contains(dest_sido_sel, na=False)]
    if "DEST_SIGU" in df.columns and dest_sigu_sel != "전체":
        df = df[df["DEST_SIGU"].astype(str).str.contains(dest_sigu_sel, na=False)]

    # 권역(수도권 inner/outer) 필터 — sido 컬럼 기반
    if origin_scope == "inner" and "SIDO" in df.columns:
        df = df[df["SIDO"].isin(INNER_SIDOS)]
    elif origin_scope == "outer" and "SIDO" in df.columns:
        df = df[~df["SIDO"].isin(INNER_SIDOS)]
    if dest_scope == "inner" and "DEST_SIDO" in df.columns:
        df = df[df["DEST_SIDO"].isin(INNER_SIDOS)]
    elif dest_scope == "outer" and "DEST_SIDO" in df.columns:
        df = df[~df["DEST_SIDO"].isin(INNER_SIDOS)]

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
    dest_sido_sel: str = "전체",
    dest_sigu_sel: str = "전체",
    origin_scope: str | None = None,
    dest_scope: str | None = None,
    pre_aggregate_level: str | None = None,
) -> tuple[pd.DataFrame, list[int]]:
    # OD 데이터: SQL 단계 필터·집계로 행수 1.5M → 수십~수백 행으로 축소 (성능 최적화)
    df = load_sheet_compat(
        backend,
        file_label,
        tab,
        origin_sido=None if sido_sel == "전체" else sido_sel,
        origin_sigu=None if sigu_sel == "전체" else sigu_sel,
        dest_sido=None if dest_sido_sel == "전체" else dest_sido_sel,
        dest_sigu=None if dest_sigu_sel == "전체" else dest_sigu_sel,
        origin_scope=origin_scope,
        dest_scope=dest_scope,
        pre_aggregate_level=pre_aggregate_level,
    )
    df = preprocess(
        df,
        backend=backend,
        query=query,
        sido_sel=sido_sel,
        sigu_sel=sigu_sel,
        dest_sido_sel=dest_sido_sel,
        dest_sigu_sel=dest_sigu_sel,
        origin_scope=origin_scope,
        dest_scope=dest_scope,
    )
    return interpolate_years(df, target_years)


def extract_years_from_query(query: str) -> list[int]:
    # \b 사용 시 한글-숫자 사이 boundary 미인식 → "2023년" 못 잡음. 단순 패턴 사용.
    years = set(int(y) for y in re.findall(r"(?<![0-9])(20\d{2})(?![0-9])", query))
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

    # "장래" 키워드 → 2025~2050 5년 단위 (배포연도 6개)
    if any(t in query for t in ["장래", "장래연도", "장래 연도"]):
        years.update([2025, 2030, 2035, 2040, 2045, 2050])

    # 명시 라벨 패턴: "기준연도 2024", "중간목표① 2031", "최종목표연도 2040", "분석연도 2027"
    for pat in [
        r"기준\s*연도\s*[:：]?\s*(20\d{2})",
        r"중간\s*목표[①②③\s]*[:：]?\s*(20\d{2})",
        r"최종\s*목표(?:\s*연도)?\s*[:：]?\s*(20\d{2})",
        r"분석\s*연도\s*[:：]?\s*(20\d{2})",
    ]:
        for m in re.finditer(pat, query):
            years.add(int(m.group(1)))

    return sorted(years)


def detect_aggregation(query: str) -> str | None:
    # 도착지 키워드 우선 — "도착지 시군구" / "도착지별 시도" 등
    has_dest = any(k in query for k in ["도착지", "도착시", "도착 시", "목적지"])
    if has_dest:
        if any(k in query for k in ["시군구", "구별", "시별"]):
            return "도착시군구"
        if any(k in query for k in ["시도", "도별"]):
            return "도착시도"
    if any(k in query for k in ["시군구별", "시군구 별"]):
        return "시군구"
    if any(k in query for k in ["시도별", "시도 별", "지역별", "지역 별", "지역 분포", "지역분포"]):
        return "시도"
    return None


def detect_top_n(query: str) -> int | None:
    """상위 N개 정렬 요청 파싱.
    - 명시 N (TOP 20, 상위 10개, 20개로 정리 등) → 정확한 N
    - "내림차순" / "오름차순" 단독 → 기본 20개
    """
    for pat in [
        r"(?:TOP|상위|내림차순|오름차순)\s*(\d+)",
        r"많은\s*(?:[가-힣]+\s*){0,5}(\d{1,3})\s*개",
        r"높은\s*(?:[가-힣]+\s*){0,5}(\d{1,3})\s*개",
        r"(\d{1,3})\s*개\s*(?:로|으로)\s*(?:정리|뽑|추출|보여)",
    ]:
        m = re.search(pat, query, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 1000:
                return n
    # N 미명시 + 정렬 의도만 있는 경우 → 기본 20
    if any(t in query for t in ["내림차순", "오름차순"]):
        return 20
    return None


def aggregate_by_region(df: pd.DataFrame, level: str) -> pd.DataFrame:
    if level not in df.columns:
        return df
    numeric_cols = [c for c in df.select_dtypes(include="number").columns if c not in {"존번호", "발생존", "도착존"}]
    if not numeric_cols:
        return df
    return df.groupby(level, dropna=False)[numeric_cols].sum().reset_index()


def get_user_years(year_inputs: list[str]) -> list[int]:
    # 사이드바 입력은 모두 제거됨. 빈 입력은 빈 리스트 반환 → 채팅 추출 연도만 사용.
    # (옛 fallback `else DIST_YEARS`는 7개 연도 강제로 늘려 무거운 쿼리 발생.)
    result = []
    for raw in year_inputs:
        value = raw.strip() if raw else ""
        if value.isdigit():
            result.append(int(value))
    return sorted(set(result))


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

    male_kw = any(t in query for t in ["남성", "남자"])
    female_kw = any(t in query for t in ["여성", "여자"])

    if any(token in query for token in ["인구", "인구수", "고령"]):
        # 연령 그룹 우선
        if any(t in query for t in ["65세이상", "65세 이상", "65+", "고령"]):
            _append_combo(combos, ("사회경제지표", "POP_65P"))
        elif any(t in query for t in ["0-14", "0~14", "유소년", "어린이"]):
            _append_combo(combos, ("사회경제지표", "POP_0_14"))
        elif any(t in query for t in ["15-64", "15~64", "생산가능"]):
            _append_combo(combos, ("사회경제지표", "POP_15_64"))
        elif any(t in query for t in ["5-24", "5~24", "청년", "유년"]):
            _append_combo(combos, ("사회경제지표", "POP_YNG"))
        elif any(t in query for t in ["15세이상", "15세 이상"]):
            _append_combo(combos, ("사회경제지표", "POP_15P"))
        else:
            # 성별 분리 우선, 없으면 총합
            if male_kw and not female_kw:
                _append_combo(combos, ("사회경제지표", "POP_MALE"))
            elif female_kw and not male_kw:
                _append_combo(combos, ("사회경제지표", "POP_FEMALE"))
            elif male_kw and female_kw:
                _append_combo(combos, ("사회경제지표", "POP_MALE"))
                _append_combo(combos, ("사회경제지표", "POP_FEMALE"))
            else:
                _append_combo(combos, ("사회경제지표", "POP_TOT"))

    if "취업자" in query:
        if male_kw and not female_kw:
            _append_combo(combos, ("사회경제지표", "EMP_MALE"))
        elif female_kw and not male_kw:
            _append_combo(combos, ("사회경제지표", "EMP_FEMALE"))
        elif male_kw and female_kw:
            _append_combo(combos, ("사회경제지표", "EMP_MALE"))
            _append_combo(combos, ("사회경제지표", "EMP_FEMALE"))
        else:
            _append_combo(combos, ("사회경제지표", "EMP"))

    if any(token in query for token in ["학생", "재학생", "학교급"]):
        # 학교급 분리 (5종)
        level_routed = False
        if any(t in query for t in ["초등", "초등학생", "초등학교"]):
            _append_combo(combos, ("사회경제지표", "STU_ELEM"))
            level_routed = True
        if any(t in query for t in ["중학", "중학생", "중학교"]):
            _append_combo(combos, ("사회경제지표", "STU_MID"))
            level_routed = True
        if any(t in query for t in ["고등", "고등학생", "고등학교"]):
            _append_combo(combos, ("사회경제지표", "STU_HIGH"))
            level_routed = True
        if any(t in query for t in ["특수학교", "특수학생"]):
            _append_combo(combos, ("사회경제지표", "STU_SPEC"))
            level_routed = True
        if any(t in query for t in ["대학생", "대학교", "대학"]):
            _append_combo(combos, ("사회경제지표", "STU_UNIV"))
            level_routed = True
        # "학교급별" 명시 → 5종 모두
        if any(t in query for t in ["학교급별", "학교급 별", "학교급"]):
            for code in ["STU_ELEM", "STU_MID", "STU_HIGH", "STU_SPEC", "STU_UNIV"]:
                _append_combo(combos, ("사회경제지표", code))
            level_routed = True
        if not level_routed:
            _append_combo(combos, ("사회경제지표", "STU"))

    if "종사자" in query:
        if any(t in query for t in ["3차", "3차산업", "삼차"]):
            _append_combo(combos, ("사회경제지표", "WORK_3RD"))
            # 3차/총 비교 키워드 시 둘 다
            if any(t in query for t in ["총종사자", "총 종사자", "총/3차", "3차/총", "비중"]):
                _append_combo(combos, ("사회경제지표", "WORK_TOT"))
        else:
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


def _is_year_column(col: str) -> tuple[int | None, str]:
    """컬럼명에서 연도 추출. 매칭되지 않으면 (None, col)."""
    m = re.search(r"(\d{4})", str(col))
    if m:
        return int(m.group(1)), col
    return None, col


def add_ratio_columns(df: pd.DataFrame, query: str) -> pd.DataFrame:
    """5목적 / 10수단 등의 row 합계 대비 비율 컬럼 추가.

    트리거: query에 "비율", "분담률", "비중", "%" 키워드.
    같은 연도(또는 단일 연도) 내 카테고리 metric 컬럼들이 합계 100%가 되도록 처리.
    """
    if not any(k in query for k in ["비율", "분담률", "비중", "%"]):
        return df
    if df is None or df.empty:
        return df

    df = df.copy()
    metric_cols = [c for c in df.select_dtypes(include="number").columns if c not in {"존번호", "발생존", "도착존"}]
    if not metric_cols:
        return df

    # 연도별 그룹화: "출근 (2023년)", "귀가 (2023년)" → 2023 그룹
    by_year: dict[int | None, list[str]] = {}
    for c in metric_cols:
        y, _ = _is_year_column(c)
        by_year.setdefault(y, []).append(c)

    for y, cols in by_year.items():
        if len(cols) < 2:
            continue  # 비율 계산 의미 없음 (단일 metric)
        row_sum = df[cols].sum(axis=1)
        for c in cols:
            ratio_col = f"{c} 비율(%)"
            df[ratio_col] = (df[c] / row_sum.mask(row_sum == 0) * 100).round(2)
    return df


def add_change_columns(df: pd.DataFrame, query: str) -> pd.DataFrame:
    """연도 간 증감률(%) 컬럼 추가.

    트리거: "증감률" / "변화율" / "변화" / "대비".
    - "연도별 증감률" 키워드 → 인접 연도 쌍 모두 (예: 2023→2025, 2025→2030, ...)
    - 그 외 → 첫 연도 vs 마지막 연도 단일 비교
    """
    if not any(k in query for k in ["증감률", "변화율", "변화", "대비"]):
        return df
    if df is None or df.empty:
        return df

    df = df.copy()
    metric_cols = [c for c in df.select_dtypes(include="number").columns if c not in {"존번호", "발생존", "도착존"}]
    if not metric_cols:
        return df

    by_metric: dict[str, list[tuple[int, str]]] = {}
    for c in metric_cols:
        y, _ = _is_year_column(c)
        if y is None:
            continue
        base = re.sub(r"\s*\(?\s*\d{4}\s*년?\s*\)?\s*", "", c).strip() or c
        by_metric.setdefault(base, []).append((y, c))

    use_adjacent = any(t in query for t in ["연도별 증감률", "연도 별 증감률", "연도별 변화", "연도별 변화율"])

    for base, items in by_metric.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda x: x[0])
        if use_adjacent:
            for (y_a, col_a), (y_b, col_b) in zip(items, items[1:]):
                change_col = f"{base} {y_a}→{y_b} 증감률(%)"
                df[change_col] = ((df[col_b] - df[col_a]) / df[col_a].mask(df[col_a] == 0) * 100).round(2)
        else:
            y_a, col_a = items[0]
            y_b, col_b = items[-1]
            change_col = f"{base} {y_a}→{y_b} 증감률(%)"
            df[change_col] = ((df[col_b] - df[col_a]) / df[col_a].mask(df[col_a] == 0) * 100).round(2)
    return df


def apply_unit_scale(df: pd.DataFrame, query: str) -> tuple[pd.DataFrame, str | None]:
    """단위 자동 변환 (천/백만/억). metric 컬럼 값을 나누고 컬럼명에 단위 접미사 표시.

    트리거:
      - "천통행" / "천명" / "천 단위" / "/천" → ÷1,000
      - "백만" → ÷1,000,000
      - "억" → ÷100,000,000
    """
    if df is None or df.empty:
        return df, None

    if any(t in query for t in ["천통행", "천 통행", "천명", "천 단위", "/천", "단위:천"]):
        scale, label = 1000, "천"
    elif any(t in query for t in ["백만통행", "백만 단위", "백만명"]):
        scale, label = 1_000_000, "백만"
    elif any(t in query for t in ["억통행", "억 단위", "억명"]):
        scale, label = 100_000_000, "억"
    else:
        return df, None

    df = df.copy()
    metric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c not in {"존번호", "발생존", "도착존"} and "비율" not in c and "증감률" not in c and "CAGR" not in c
    ]
    rename_map = {}
    for c in metric_cols:
        df[c] = (df[c] / scale).round(3)
        rename_map[c] = f"{c} ({label})"
    if rename_map:
        df.rename(columns=rename_map, inplace=True)
    return df, label


def add_cagr_columns(df: pd.DataFrame, query: str) -> pd.DataFrame:
    """3개 이상 연도 컬럼이 있을 때 연평균 증가율(CAGR) 컬럼 추가.

    트리거: "CAGR" / "연평균" / "연평균 증가".
    """
    if not any(k in query for k in ["CAGR", "cagr", "연평균"]):
        return df
    if df is None or df.empty:
        return df

    df = df.copy()
    metric_cols = [c for c in df.select_dtypes(include="number").columns if c not in {"존번호", "발생존", "도착존"}]
    by_metric: dict[str, list[tuple[int, str]]] = {}
    for c in metric_cols:
        y, _ = _is_year_column(c)
        if y is None:
            continue
        base = re.sub(r"\s*\(?\s*\d{4}\s*년?\s*\)?\s*", "", c).strip() or c
        by_metric.setdefault(base, []).append((y, c))

    for base, items in by_metric.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda x: x[0])
        y_a, col_a = items[0]
        y_b, col_b = items[-1]
        n = y_b - y_a
        if n <= 0:
            continue
        cagr_col = f"{base} {y_a}~{y_b} CAGR(%)"
        ratio = df[col_b] / df[col_a].mask(df[col_a] == 0)
        df[cagr_col] = ((ratio ** (1 / n) - 1) * 100).round(3)
    return df


def _pick_sort_column(df: pd.DataFrame, query: str) -> str | None:
    """TOP N 정렬 시 사용할 metric 컬럼 선택. query 키워드와 컬럼명 매칭 우선."""
    candidates = [
        c for c in df.select_dtypes(include="number").columns
        if c not in {"존번호", "발생존", "도착존"}
    ]
    if not candidates:
        return None
    # query에 등장하는 단어와 일치하는 metric 컬럼 우선 (예: "출근" → "출근 (2023년)")
    for col in candidates:
        base = re.sub(r"\s*\([^)]*\)\s*", "", col).strip()
        if base and base in query:
            return col
    # fallback: 합계 최대인 컬럼
    valid = [c for c in candidates if df[c].fillna(0).sum() > 0]
    if valid:
        return max(valid, key=lambda c: df[c].fillna(0).sum())
    return None


def apply_top_n(tables: list[dict], n: int, query: str) -> list[dict]:
    """각 결과 표에 대해 metric 컬럼 기준 내림차순 + head(n)."""
    for t in tables:
        df = t["df"]
        if df is None or df.empty:
            continue
        sort_col = _pick_sort_column(df, query)
        if sort_col is None:
            continue
        t["df"] = df.sort_values(sort_col, ascending=False, na_position="last").head(n).reset_index(drop=True)
        suffix = f" `{sort_col}` 기준 상위 {n}개로 정렬."
        t["note"] = ((t.get("note") or "") + suffix).strip()
    return tables


def needs_llm_summary(query: str) -> bool:
    """질의에 보고서/분석/문장 키워드가 있으면 LLM 요약 호출."""
    return any(k in query for k in ["문장", "보고서", "분석", "요약", "설명", "해설", "기술", "서술"])


def generate_llm_summary(model_, tables: list[dict], query: str) -> str:
    """표 내용을 LLM에 넘겨 한국어 보고서 문장 4~5줄 생성.

    절대 룰 5: 표는 SQL/pandas로 결정론적 생성, LLM은 표 기반 해설만.
    프롬프트에 명시적으로 수치 변경 금지 명령 포함.
    """
    if model_ is None or not tables:
        return ""
    md_parts = []
    for t in tables:
        title = t.get("title") or "결과"
        df = t["df"]
        if df is None or df.empty:
            continue
        if len(df) > 30:
            md = df.head(30).to_markdown(index=False) + f"\n\n_(총 {len(df)}행 중 상위 30행만 컨텍스트로 제공)_"
        else:
            md = df.to_markdown(index=False)
        md_parts.append(f"### {title}\n\n{md}")
    if not md_parts:
        return ""
    table_context = "\n\n".join(md_parts)
    prompt = f"""다음은 KTDB 데이터를 SQL/pandas로 결정론적 집계한 결과입니다.
사용자 질의에 맞춰 보고서에 들어갈 분석 문장을 작성해주세요.

엄수 규칙:
1. 표의 수치를 변경하거나 새로 만들지 마세요. 표에 없는 값은 절대 추정·생성 금지.
2. 추세, 격차, 상위/하위 사례, 특이점 위주로 해설.
3. 한국어 보고서 문체 (평어체, "~한 것으로 나타났다" 등).
4. 4~5문장.

사용자 질의: {query}

표:
{table_context}

분석 문장:"""
    try:
        response = model_.generate_content(prompt)
        return (response.text or "").strip()
    except Exception as e:
        return f"_(자연어 요약 실패: {str(e)[:120]})_"


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

    sido_sel = "전체"
    sigu_sel = "전체"
    year_base = year_mid1 = year_mid2 = year_mid3 = year_final = ""

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
        top_n = detect_top_n(user_input)
        (o_sido, o_sigu), (d_sido, d_sigu) = extract_od_pair_from_query(backend, user_input)
        origin_scope, dest_scope = detect_region_scope(user_input)
        auto_sido, auto_sigu = (o_sido, o_sigu)

        # OD 사전 집계 결정 — 도착지 분석 의도가 없고 origin/시군구 단위 합계만 원할 때
        # GROUP BY를 SQL에 푸시다운해 1.5M → 수십 행으로 축소.
        def _decide_od_pre_agg() -> str | None:
            if agg_level == "도착시군구":
                return "dest_sigu"
            if agg_level == "도착시도":
                return "dest_sido"
            # 도착지 필터/그룹 의도가 있으면 raw 페치 (도착지 정보 보존 필요)
            if d_sido or d_sigu or dest_scope:
                return None
            if agg_level == "시군구":
                return "origin_sigu"
            if agg_level == "시도":
                return "origin_sido"
            return None

        od_pre_agg = _decide_od_pre_agg()

        with st.spinner(f"데이터 로딩 중... ({len(combos)}개)"):
            try:
                for file_label, tab in combos:
                    # 사회경제지표는 pre_aggregate 미지원 (load_socio_indicator는 자체 파이프라인)
                    pre_agg_for_combo = od_pre_agg if file_label != "사회경제지표" else None
                    df, interp = load_integrated(
                        backend,
                        file_label,
                        tab,
                        target_years,
                        user_input,
                        sido_sel=o_sido or "전체",
                        sigu_sel=o_sigu or "전체",
                        dest_sido_sel=d_sido or "전체",
                        dest_sigu_sel=d_sigu or "전체",
                        origin_scope=origin_scope,
                        dest_scope=dest_scope,
                        pre_aggregate_level=pre_agg_for_combo,
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
        # 후처리 순서: 비율 → 증감률 → CAGR → 단위 변환
        # (단위 변환은 raw count metric에만 적용; 비율/증감률 컬럼은 원래 무단위 %)
        unit_label = None
        for _t in tables:
            _t["df"] = add_ratio_columns(_t["df"], user_input)
            _t["df"] = add_change_columns(_t["df"], user_input)
            _t["df"] = add_cagr_columns(_t["df"], user_input)
            _t["df"], _u = apply_unit_scale(_t["df"], user_input)
            if _u and not unit_label:
                unit_label = _u
        if top_n:
            tables = apply_top_n(tables, top_n, user_input)

        summary = build_summary(
            backend=backend,
            tables=tables,
            combos=combos,
            interp_years=sorted(interp_years),
            agg_level=agg_level,
            auto_sido=auto_sido,
            auto_sigu=auto_sigu,
        )
        if d_sido or d_sigu:
            summary += f"\n- 도착지 필터: {d_sigu or d_sido}"
        if top_n:
            summary += f"\n- 정렬: 상위 {top_n}개"
        st.markdown(summary)

        # LLM 자연어 요약 (요청 시) — 표는 결정론, LLM은 해설만
        llm_text = ""
        if needs_llm_summary(user_input):
            if model is None:
                st.markdown("---")
                st.info(
                    "보고서 문장 자동 생성 기능은 `GEMINI_API_KEY`가 필요합니다. "
                    "`.streamlit/secrets.toml`에 키를 채운 뒤 streamlit을 재시작해주세요."
                )
            else:
                with st.spinner("보고서 문장 생성 중..."):
                    llm_text = generate_llm_summary(model, tables, user_input)
                if llm_text:
                    st.markdown("---")
                    st.markdown(f"**📋 보고서용 분석 문장**\n\n{llm_text}")

        render_tables(tables, len(st.session_state.messages))
        full_content = summary + (("\n\n---\n\n**📋 보고서용 분석 문장**\n\n" + llm_text) if llm_text else "")
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": full_content,
                "tables": tables,
            }
        )
