import io
import json
import re
import time

import google.generativeai as genai
import pandas as pd
import psycopg2
import streamlit as st

# ─────────────────────────────────────────────────────────────
# 1. 페이지 설정
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="KTDB 통합 분석 에이전트", layout="wide")

st.markdown("""
<style>
thead tr th { background: #f0f2f6; font-weight: 600; }
.stDataFrame { font-size: 13px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# 2. AI 모델 초기화
# ─────────────────────────────────────────────────────────────
@st.cache_resource
def init_model():
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    return genai.GenerativeModel("gemini-2.5-pro")

try:
    model = init_model()
except Exception as e:
    st.error(f"AI 모델 초기화 실패: {e}")
    st.stop()

# ─────────────────────────────────────────────────────────────
# 3. Supabase Postgres 연결 (dead connection 자동 재연결)
# ─────────────────────────────────────────────────────────────
@st.cache_resource
def _connect_db():
    sb = st.secrets["supabase"]
    return psycopg2.connect(
        host=sb["host"], port=sb["port"], database=sb["database"],
        user=sb["user"], password=sb["password"], sslmode="require",
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
    )

def init_db():
    """살아있는 connection 반환. 끊어진 경우 자동 재연결."""
    conn = _connect_db()
    try:
        if conn.closed:
            raise psycopg2.OperationalError("connection closed")
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError):
        try:
            conn.close()
        except Exception:
            pass
        _connect_db.clear()
        return _connect_db()

try:
    conn = init_db()
except Exception as e:
    st.error(f"DB 연결 실패: {e}")
    st.stop()

# ─────────────────────────────────────────────────────────────
# 4. 데이터 모델 정의 (시트 호환 인터페이스 유지)
# ─────────────────────────────────────────────────────────────
YEARS = ["2023", "2025", "2030", "2035", "2040", "2045", "2050"]

SHEET_CONFIG = {
    "사회경제지표": {
        "tabs": {
            "ZONE":     "존체계(행정구역)",
            "POP_TOT":  "총 인구수",
            "POP_YNG":  "5-24세 인구수",
            "POP_15P":  "15세이상 인구수",
            "EMP":      "취업자수",
            "STU":      "수용학생수",
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
    "SIDO": "시도", "SIGU": "시군구", "ZONE": "존번호",
    "ORGN": "발생존", "DEST": "도착존",
    "DEST_SIDO": "도착시도", "DEST_SIGU": "도착시군구",
    "2023": "2023년", "2025": "2025년", "2030": "2030년",
    "2035": "2035년", "2040": "2040년", "2045": "2045년", "2050": "2050년",
    "WORK": "출근", "SCHO": "등교", "BUSI": "업무", "HOME": "귀가", "OTHE": "기타",
    "AUTO": "승용차", "OBUS": "버스", "SUBW": "지하철",
    "RAIL": "일반철도", "ERAI": "고속철도",
    "ATT_AANT": "승용차(접근)", "ATT_OBUS": "버스(접근)",
}

UNITS = {
    "사회경제지표": "명",
    "목적OD":      "통행/일",
    "주수단OD":    "통행/일",
    "접근수단OD":  "통행/일",
}

# ─────────────────────────────────────────────────────────────
# 5. DB 데이터 로드 함수
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def load_zones() -> pd.DataFrame:
    df = pd.read_sql("SELECT zone AS \"ZONE\", sido AS \"SIDO\", sigu AS \"SIGU\" FROM zones ORDER BY zone",
                     init_db())
    return df


@st.cache_data(ttl=600, show_spinner=False)
def load_socio_indicator(indicator_code: str) -> pd.DataFrame:
    """사회경제지표를 wide format(연도 컬럼)으로 반환."""
    sql = """
        SELECT z.sido AS "SIDO", z.sigu AS "SIGU", s.zone AS "ZONE",
               s.year, s.value
        FROM socio s JOIN zones z USING (zone)
        WHERE s.indicator_code = %s
        ORDER BY s.zone, s.year
    """
    df = pd.read_sql(sql, init_db(), params=(indicator_code,))
    if df.empty:
        return df
    wide = df.pivot_table(index=["SIDO", "SIGU", "ZONE"], columns="year",
                          values="value", aggfunc="first").reset_index()
    wide.columns = [str(c) for c in wide.columns]
    wide.columns.name = None
    return wide


_OD_COLS = {
    "od_purpose":     ["work", "scho", "busi", "home", "othe"],
    "od_main_mode":   ["auto", "obus", "subw", "rail", "erai"],
    "od_access_mode": ["att_aant", "att_obus"],
}

@st.cache_data(ttl=600, show_spinner=False)
def load_od(table: str, year: int) -> pd.DataFrame:
    """OD 데이터를 ORGN/DEST 양쪽 zones와 머지하여 반환."""
    cols = _OD_COLS[table]
    cols_sql = ", ".join(f"t.{c}" for c in cols)
    sql = f"""
        SELECT zo.sido AS "SIDO",      zo.sigu AS "SIGU",
               t.orgn  AS "ORGN",      t.dest  AS "DEST",
               zd.sido AS "DEST_SIDO", zd.sigu AS "DEST_SIGU",
               {cols_sql}
        FROM {table} t
        LEFT JOIN zones zo ON t.orgn = zo.zone
        LEFT JOIN zones zd ON t.dest = zd.zone
        WHERE t.year = %s
    """
    df = pd.read_sql(sql, init_db(), params=(year,))
    fixed = {"SIDO", "SIGU", "ORGN", "DEST", "DEST_SIDO", "DEST_SIGU"}
    df.columns = [c if c in fixed else c.upper() for c in df.columns]
    return df


def load_sheet_compat(file_label: str, tab: str) -> pd.DataFrame:
    """SHEET_CONFIG의 file/tab 키를 받아 해당 데이터를 반환."""
    if file_label == "사회경제지표":
        if tab == "ZONE":
            return load_zones()
        return load_socio_indicator(tab)
    if file_label == "목적OD":
        year = int(tab.split("_")[1])
        return load_od("od_purpose", year)
    if file_label == "주수단OD":
        year = int(tab.split("_")[1])
        return load_od("od_main_mode", year)
    if file_label == "접근수단OD":
        return load_od("od_access_mode", 2023)
    raise ValueError(f"알 수 없는 시트 매핑: {file_label}/{tab}")


# ─────────────────────────────────────────────────────────────
# 5-bis. 채팅 본문에서 시도/시군구 자동 추출
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def get_all_regions() -> tuple[list, list]:
    df = pd.read_sql("SELECT DISTINCT sido, sigu FROM zones", init_db())
    return (sorted(df["sido"].dropna().unique().tolist(), key=len, reverse=True),
            sorted(df["sigu"].dropna().unique().tolist(), key=len, reverse=True))


def extract_region_from_query(query: str) -> tuple[str | None, str | None]:
    """채팅 본문에서 시도/시군구 키워드 자동 추출. 더 긴 이름부터 매칭."""
    if not query:
        return None, None
    sidos, sigus = get_all_regions()
    matched_sido = next((s for s in sidos if s in query), None)
    matched_sigu = next((s for s in sigus if s in query), None)
    return matched_sido, matched_sigu


# ─────────────────────────────────────────────────────────────
# 6. 시군구 목록 동적 로드
# ─────────────────────────────────────────────────────────────
SIDO_LIST = [
    "전체", "서울특별시", "부산광역시", "대구광역시", "인천광역시",
    "광주광역시", "대전광역시", "울산광역시", "세종특별자치시", "경기도",
    "강원특별자치도", "충청북도", "충청남도", "전북특별자치도", "전라남도",
    "경상북도", "경상남도", "제주특별자치도"
]

@st.cache_data(ttl=600, show_spinner=False)
def get_sigu_list(sido: str) -> list:
    try:
        if sido == "전체":
            df = pd.read_sql("SELECT DISTINCT sigu FROM zones ORDER BY sigu", init_db())
        else:
            df = pd.read_sql(
                "SELECT DISTINCT sigu FROM zones WHERE sido LIKE %s ORDER BY sigu",
                init_db(), params=(f"%{sido}%",))
        return ["전체"] + df["sigu"].dropna().tolist()
    except Exception:
        return ["전체"]


# ─────────────────────────────────────────────────────────────
# 7. 세션 상태 초기화
# ─────────────────────────────────────────────────────────────
for key, default in {
    "messages":    [],
    "sel_file":    None,
    "sel_tab":     None,
    "transpose":   False,
    "manual_mode": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ─────────────────────────────────────────────────────────────
# 8. 사이드바
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 분석 조건")
    st.caption("모든 항목은 선택사항입니다. 미입력 시 전체 데이터를 대상으로 분석합니다.")

    st.subheader("📍 분석 대상 지역")
    sido_sel     = st.selectbox("시도", SIDO_LIST)
    sigu_options = get_sigu_list(sido_sel)
    sigu_sel     = st.selectbox("시군구", sigu_options)

    st.divider()
    st.subheader("📅 분석 연도")
    st.caption("배포 연도(2023·2025·2030·2035·2040·2045·2050) 외 입력 시 보간법 적용")
    year_base  = st.text_input("기준연도",    placeholder="예: 2023  (선택)")
    col1, col2 = st.columns(2)
    with col1:
        year_mid1 = st.text_input("중간목표①", placeholder="예: 2030")
    with col2:
        year_mid2 = st.text_input("중간목표②", placeholder="예: 2040")
    year_mid3  = st.text_input("중간목표③",   placeholder="예: 2045  (선택)")
    year_final = st.text_input("최종목표연도", placeholder="예: 2050  (선택)")

    st.divider()
    st.subheader("📂 시트 선택")
    manual_mode = st.toggle(
        "직접 선택 (OFF = AI 자동)",
        value=st.session_state.manual_mode,
        help="OFF: 질문에 따라 AI가 시트를 자동 선택합니다.\nON: 아래에서 직접 선택한 시트를 우선합니다."
    )
    st.session_state.manual_mode = manual_mode

    if manual_mode:
        file_opts   = list(SHEET_CONFIG.keys())
        file_labels = ["— 파일을 선택하세요 —"] + file_opts
        current_file_idx = (
            file_labels.index(st.session_state.sel_file)
            if st.session_state.sel_file in file_labels else 0
        )
        sel_file_label = st.selectbox("파일", file_labels, index=current_file_idx)

        if sel_file_label == "— 파일을 선택하세요 —":
            st.session_state.sel_file = None
            st.session_state.sel_tab  = None
            st.caption("⬆️ 파일을 먼저 선택하세요.")
        else:
            st.session_state.sel_file = sel_file_label
            tab_opts   = list(SHEET_CONFIG[sel_file_label]["tabs"].keys())
            tab_labels = ["— 시트를 선택하세요 —"] + [
                f"{k} — {v}" for k, v in SHEET_CONFIG[sel_file_label]["tabs"].items()
            ]
            current_tab_display = (
                f"{st.session_state.sel_tab} — {SHEET_CONFIG[sel_file_label]['tabs'].get(st.session_state.sel_tab, '')}"
                if st.session_state.sel_tab in tab_opts else "— 시트를 선택하세요 —"
            )
            current_tab_idx = (
                tab_labels.index(current_tab_display)
                if current_tab_display in tab_labels else 0
            )
            sel_tab_label = st.selectbox("시트(탭)", tab_labels, index=current_tab_idx)

            if sel_tab_label == "— 시트를 선택하세요 —":
                st.session_state.sel_tab = None
                st.caption("⬆️ 시트를 선택하세요.")
            else:
                sel_tab = tab_opts[tab_labels.index(sel_tab_label) - 1]
                st.session_state.sel_tab = sel_tab
                st.caption(f"✅ 고정: `{sel_file_label}` > `{sel_tab}`")
    else:
        st.caption("🤖 AI가 질문을 분석해 시트를 자동 선택합니다.")
        if st.session_state.sel_file and st.session_state.sel_tab:
            st.caption(f"마지막 선택: `{st.session_state.sel_file}` > `{st.session_state.sel_tab}`")

    st.divider()
    with st.expander("🔄 데이터 동기화 (시트→DB)"):
        st.caption("Google Sheets의 최신 데이터를 Supabase로 재적재합니다. 약 1~2분 소요.")
        if st.button("동기화 실행"):
            from sync_sheets_to_db import sync_all
            urls = {
                "socio":   st.secrets["SHEET_URL_SOCIO"],
                "obj_od":  st.secrets["SHEET_URL_OBJ_OD"],
                "main_od": st.secrets["SHEET_URL_MAIN_OD"],
                "acc_od":  st.secrets["SHEET_URL_ACC_OD"],
            }
            with st.spinner("동기화 중..."):
                report = sync_all(init_db(), urls)
                st.cache_data.clear()
            for r in report:
                if r.get("rows", 0) > 0:
                    st.success(f"{r['table']}: {r['rows']:,}행 ({r['duration_s']}s)")
                else:
                    st.error(f"{r['table']}: {r.get('error', '실패')}")

    if st.button("🗑️ 대화 초기화"):
        st.session_state.messages = []
        st.rerun()

# ─────────────────────────────────────────────────────────────
# 9. AI 탭 자동 선택
# ─────────────────────────────────────────────────────────────
def ai_route(query: str) -> list[tuple[str, str]]:
    """질문 답변에 필요한 (file, tab) 조합을 1개 이상 반환. 최대 8개."""
    registry = {fname: list(cfg["tabs"].keys()) for fname, cfg in SHEET_CONFIG.items()}
    prompt = f"""
KTDB 시트 구성:
{json.dumps(registry, ensure_ascii=False)}

사용자 질문: "{query}"

질문에 답하기 위해 필요한 모든 (file, tab) 조합을 JSON 배열로만 반환하세요.
규칙:
- 단일 지표/단일 연도 → 1개 반환
- 여러 지표 (예: 인구+종사자) → 각 지표 탭 추가
- 여러 연도 (예: 2023~2050) → 해당 연도 탭들 추가
- 최대 8개

예: [{{"file": "사회경제지표", "tab": "POP_TOT"}}, {{"file": "사회경제지표", "tab": "WORK_TOT"}}]

JSON 외 텍스트 금지.
"""
    try:
        raw = model.generate_content(prompt).text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        if isinstance(result, dict):
            result = [result]
        valid = []
        seen = set()
        for item in result:
            f, t = item.get("file"), item.get("tab")
            if f in SHEET_CONFIG and t in SHEET_CONFIG[f]["tabs"] and (f, t) not in seen:
                valid.append((f, t))
                seen.add((f, t))
        if valid:
            return valid[:8]
    except Exception:
        pass
    return [(list(SHEET_CONFIG.keys())[0], "POP_TOT")]

# ─────────────────────────────────────────────────────────────
# 10. 전처리 (시도/시군구 필터링 + 한글 컬럼명 변환)
#     사이드바 선택 우선, 미선택 시 채팅 본문에서 자동 추출
#     ORGN→ZONE 머지는 SQL JOIN에서 이미 처리됨
# ─────────────────────────────────────────────────────────────
def preprocess(df: pd.DataFrame, query: str = "") -> pd.DataFrame:
    df = df.copy()

    # 사이드바 입력 우선, 미선택("전체") 항목은 채팅에서 자동 추출
    sido_eff, sigu_eff = sido_sel, sigu_sel
    if sido_eff == "전체" or sigu_eff == "전체":
        auto_sido, auto_sigu = extract_region_from_query(query)
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

# ─────────────────────────────────────────────────────────────
# 11. 보간법
# ─────────────────────────────────────────────────────────────
DIST_YEARS = [int(y) for y in YEARS]

def get_user_years() -> list[int]:
    raw    = [year_base, year_mid1, year_mid2, year_mid3, year_final]
    result = []
    for y in raw:
        y = y.strip() if y else ""
        if y.isdigit():
            result.append(int(y))
    return sorted(set(result)) if result else DIST_YEARS

def interpolate_years(df: pd.DataFrame, target_years: list[int]) -> tuple[pd.DataFrame, list[int]]:
    interp_years = []
    for y in target_years:
        col_name = f"{y}년"
        if col_name in df.columns:
            continue
        if y in DIST_YEARS:
            continue
        lower = max([d for d in DIST_YEARS if d <= y], default=None)
        upper = min([d for d in DIST_YEARS if d >= y], default=None)
        if lower and upper and lower != upper:
            lc, uc = f"{lower}년", f"{upper}년"
            if lc in df.columns and uc in df.columns:
                ratio        = (y - lower) / (upper - lower)
                df[col_name] = (
                    pd.to_numeric(df[lc], errors="coerce") +
                    ratio * (pd.to_numeric(df[uc], errors="coerce") -
                             pd.to_numeric(df[lc], errors="coerce"))
                ).round(1)
                interp_years.append(y)
    return df, interp_years

# ─────────────────────────────────────────────────────────────
# 12. 통합 로드
# ─────────────────────────────────────────────────────────────
def load_integrated(file_label: str, tab: str,
                    target_years: list[int],
                    query: str = "") -> tuple[pd.DataFrame, list[int]]:
    df = load_sheet_compat(file_label, tab)
    df = preprocess(df, query)
    df, interp = interpolate_years(df, target_years)
    return df, interp

# ─────────────────────────────────────────────────────────────
# 12-bis. 질의 분석 헬퍼
# ─────────────────────────────────────────────────────────────
def extract_years_from_query(query: str) -> list[int]:
    """채팅에서 연도 추출. 단일 연도 + 범위 + N년단위 표현 인식.
    예시:
      "2025년"           → [2025]
      "2025~2050"        → [2025, 2030, 2035, 2040, 2045, 2050] (배포 연도 모두)
      "2025~2050 5년단위" → [2025, 2030, 2035, 2040, 2045, 2050]
      "2025~2050 10년단위"→ [2025, 2035, 2045]
    """
    years = set(int(y) for y in re.findall(r"\b(20\d{2})\b", query))

    # 범위 패턴: 2025~2050, 2025-2050, 2025 to 2050
    rng = re.search(r"(20\d{2})\s*[~∼\-–]+\s*(20\d{2})", query)
    if rng:
        a, b = int(rng.group(1)), int(rng.group(2))
        lo, hi = min(a, b), max(a, b)
        # N년단위 step 인식
        step_match = re.search(r"(\d+)\s*년\s*단위", query)
        if step_match:
            step = int(step_match.group(1))
            years.update(range(lo, hi + 1, step))
        else:
            # 기본: 배포 연도 중 범위 내 모두 포함
            years.update(y for y in DIST_YEARS if lo <= y <= hi)
    return sorted(years)

def detect_aggregation(query: str) -> str | None:
    if any(k in query for k in ["시군구별", "시군구 별"]):
        return "시군구"
    if any(k in query for k in [
        "시도별", "시도 별", "지역별", "지역 별",
        "지역 분포", "지역분포", "통행분포", "통행 분포"
    ]):
        return "시도"
    return None

def detect_freight_query(query: str) -> bool:
    return any(k in query for k in ["화물", "freight", "물류"])

def aggregate_by_region(df: pd.DataFrame, level: str) -> pd.DataFrame:
    if level not in df.columns:
        return df
    skip = {"존번호", "발생존", "도착존"}
    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns if c not in skip
    ]
    if not numeric_cols:
        return df
    return df.groupby(level)[numeric_cols].sum().reset_index()

# ─────────────────────────────────────────────────────────────
# 13. AI 분석 프롬프트 규칙
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 KTDB 전문 분석가입니다. 아래 규칙을 엄격히 따르세요.

[출력 규칙]
1. 설명은 2~3줄 이내 핵심 요약만 작성.
2. 표 헤더는 한글 공식 용어 사용(시도, 시군구, 존번호, 총 인구수, 출근, 승용차 등).
3. 표는 존번호(또는 발생존) 오름차순 정렬.
4. 단위를 표 상단이나 헤더에 반드시 표기.
5. 보간 연도가 있으면 해당 열 헤더에 *(보간) 주석 추가.
6. 행정구역(시도·시군구·존번호) 컬럼은 고정, 나머지는 질문에 따라 구성.
7. 연도별 비교: 상위 헤더=항목명, 하위 헤더=연도 / 항목별 비교: 상위 헤더=연도, 하위 헤더=항목명.
8. 실제 데이터에 없는 수치를 절대 만들어내지 마세요.
9. 출력 형식: 요약 텍스트 → CSV 블록(```csv ... ```) 순.
"""

# ─────────────────────────────────────────────────────────────
# 14. 기존 대화 렌더링
# ─────────────────────────────────────────────────────────────
st.title("🚦 KTDB 통합 분석 에이전트")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "df" in msg:
            df_show = msg["df"].T if st.session_state.transpose else msg["df"]
            st.dataframe(df_show, use_container_width=True)
            csv_bytes = ("﻿" + msg["df"].to_csv(index=False)).encode("utf-8")
            st.download_button(
                "📋 CSV 다운로드",
                data=csv_bytes,
                file_name="ktdb_result.csv",
                mime="text/csv; charset=utf-8",
                key=f"dl_{id(msg)}"
            )

if any("df" in m for m in st.session_state.messages):
    st.session_state.transpose = st.toggle(
        "↔️ 행·열 전환", value=st.session_state.transpose
    )

# ─────────────────────────────────────────────────────────────
# 15. 질문 처리
# ─────────────────────────────────────────────────────────────
if user_input := st.chat_input("질문을 입력하세요 — 예: 경기도 2030년 인구수와 종사자수 비교"):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):

        # ① 시트 결정 (manual: 단일 / AI: 1~8개)
        if manual_mode and st.session_state.sel_file and st.session_state.sel_tab:
            combos = [(st.session_state.sel_file, st.session_state.sel_tab)]
            tab_kr_list = [SHEET_CONFIG[combos[0][0]]["tabs"].get(combos[0][1], combos[0][1])]
            st.caption(f"📂 직접 선택: **{combos[0][0]}** > **{tab_kr_list[0]}**")
        elif manual_mode:
            st.warning("직접 선택 모드입니다. 사이드바에서 파일과 시트를 선택해 주세요.")
            st.stop()
        else:
            with st.spinner("AI가 적합한 시트를 선택 중..."):
                combos = ai_route(user_input)
                st.session_state.sel_file = combos[0][0]
                st.session_state.sel_tab  = combos[0][1]
            tab_kr_list = [SHEET_CONFIG[f]["tabs"].get(t, t) for f, t in combos]
            display = ", ".join(f"**{f}**>**{kr}**" for (f, _), kr in zip(combos, tab_kr_list))
            st.caption(f"📂 AI 자동 선택 ({len(combos)}개): {display}")

        # ② 연도 결정 (사이드바 입력 + 채팅 본문에서 추출/범위)
        chat_years   = extract_years_from_query(user_input)
        target_years = sorted(set(get_user_years() + chat_years))

        # ③ 다중 데이터 로드
        datasets = []
        all_interp = set()
        with st.spinner(f"데이터 로딩 중... ({len(combos)}개 시트)"):
            try:
                for (f, t), tab_kr in zip(combos, tab_kr_list):
                    d, interp = load_integrated(f, t, target_years, user_input)
                    datasets.append({"file": f, "tab": t, "tab_kr": tab_kr, "df": d})
                    all_interp.update(interp)
            except Exception as e:
                st.error(f"❌ 데이터 로드 실패: {e}")
                st.stop()

        unit         = UNITS.get(datasets[0]["file"], "")
        interp_years = sorted(all_interp)
        interp_note  = (
            f"\n※ 보간 연도: {interp_years} (선형보간법 적용)"
            if interp_years else ""
        )

        # ③-bis. 화물 질의 안내
        freight_requested = detect_freight_query(user_input)
        if freight_requested:
            st.warning(
                "⚠️ 현재 연결된 KTDB 데이터는 **여객(passenger) 데이터만** 포함합니다. "
                "화물(freight) 통행량은 별도 시트가 필요하므로 본 분석에서는 제외됩니다."
            )

        # ③-ter. 지역별 집계 (각 dataset마다)
        agg_level = detect_aggregation(user_input)
        agg_note  = ""
        for d in datasets:
            df_orig = d["df"]
            if agg_level and agg_level in df_orig.columns:
                d["df_for_ai"] = aggregate_by_region(df_orig, agg_level)
            else:
                d["df_for_ai"] = df_orig
        if agg_level and any(agg_level in d["df"].columns for d in datasets):
            agg_note = f"\n※ {agg_level}별 합계 집계 적용"
            st.caption(f"📊 {agg_level}별 합계로 집계됨")

        # ④ AI 분석 (다중 데이터셋)
        max_rows = 300 if agg_level else 150
        per_ds = max(20, max_rows // max(1, len(datasets)))

        sections = []
        for d in datasets:
            sample = d["df_for_ai"].head(per_ds).to_string(index=False)
            sections.append(
                f"\n[데이터셋: {d['file']} / {d['tab_kr']}]\n"
                f"컬럼: {list(d['df_for_ai'].columns)}\n"
                f"샘플(최대 {per_ds}행):\n{sample}"
            )
        all_data_text = "\n".join(sections)

        freight_note = (
            "\n\n[중요 안내]\n"
            "본 데이터에는 화물(freight) 데이터가 존재하지 않습니다. "
            "화물 관련 수치는 절대 생성·추정하지 말고, 응답 모두에 그 사실을 명시하세요."
            if freight_requested else ""
        )
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"[데이터 정보]\n"
            f"단위(첫 데이터셋 기준): {unit} / 데이터셋 수: {len(datasets)}"
            f"{interp_note}{agg_note}\n"
            f"{all_data_text}"
            f"{freight_note}\n\n"
            f"[질문]\n{user_input}"
        )

        with st.spinner("보고서 작성 중..."):
            response = model.generate_content(prompt)

        full_text = response.text
        summary   = full_text.split("```csv")[0].strip()
        st.markdown(summary)

        new_msg = {"role": "assistant", "content": summary}

        if "```csv" in full_text:
            csv_raw = full_text.split("```csv")[1].split("```")[0].strip()
            try:
                res_df  = pd.read_csv(io.StringIO(csv_raw))
                df_show = res_df.T if st.session_state.transpose else res_df
                st.dataframe(df_show, use_container_width=True)
                csv_dl = ("﻿" + res_df.to_csv(index=False)).encode("utf-8")
                st.download_button(
                    "📋 CSV 다운로드",
                    data=csv_dl,
                    file_name="ktdb_result.csv",
                    mime="text/csv; charset=utf-8"
                )
                new_msg["df"] = res_df
            except Exception:
                st.warning("CSV 파싱 실패 — 텍스트 결과만 표시합니다.")

        st.session_state.messages.append(new_msg)
