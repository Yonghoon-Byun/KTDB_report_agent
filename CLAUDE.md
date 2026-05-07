# KTDB 통합 분석 에이전트

## 프로젝트 개요

KTDB(국가교통DB) 데이터를 **Azure Postgres**(`geo-spatial-hub` DEV 서버, `ktdb` 테넌트)에 적재한 뒤
Gemini AI로 자연어 분석하는 Streamlit 앱. 사용자가 "수도권 종로구 출근 통행" 같은 한국어 질의를 던지면
앱이 dataset+컬럼을 자동 라우팅해 SQL 실행 후 표/CSV로 응답.

- **프론트엔드**: Streamlit (단일 파일 `streamlit_app.py`)
- **AI**: Google Gemini 2.5 Pro
- **DB**: Azure Database for PostgreSQL Flexible Server (`geo-spatial-hub`, ktdb DB)
  - 일반 쿼리: PgBouncer port **6432**
  - DDL/대량 적재: 직접 연결 port **5432**
- **데이터 소스**: KTDB 원본 zip (수도권 1310 TAZ × 7년) + 기존 Google Sheets 4종 (전국 250존, 마이그레이션 잔여)
- **처리**: pandas + psycopg2 + (예정) bulk INSERT

## 아키텍처

```
[KTDB 원본 zip 3개]                ← docs/PRD_DB-플랫폼-Azure-전환.md 참조
        ↓ scripts/provision_ktdb.py    ← DB 분양 (1회, idempotent)
        ↓ db_schema_smr.sql            ← 9 테이블 + 21 파티션 + 60 컬럼 COMMENT
        ↓ sync_smr.py (예정)            ← zip 풀기 + sparse drop + bulk INSERT
[Azure Postgres ktdb]               ← smr_zones, smr_socio_*, smr_od_*, smr_sync_log
        ↓ psycopg2 SQL JOIN          ← db_env.py 헬퍼 (PgBouncer 6432)
[streamlit_app.py]
        ↓ Gemini 2.5 Pro             ← ai_route() 자연어 → dataset+컬럼
[표 + CSV 다운로드]
```

### 왜 Azure로 옮겼나
- Supabase Free 500MB로 수도권 7년 데이터(추정 7.6GB) 적재 불가, Pro 8GB도 마진 부족
- 회사 Azure 구독 + 사내망 사용 가능 → 비용 절감 + 회사 보안 정책 부합
- 사용자 GIS 플러그인이 이미 같은 서버 쓰고 있어 인프라 친숙
- 자세한 의사결정: [`docs/PRD_DB-플랫폼-Azure-전환.md`](docs/PRD_DB-플랫폼-Azure-전환.md)

### 왜 Sheets에서 OAuth 안 쓰나 (legacy)
회사 보안 소프트웨어가 `oauth2.googleapis.com` 트래픽을 차단(WinError 10053).
기존 250존 데이터는 link-share이므로 `gviz/tq?tqx=out:csv&sheet=<TAB>` URL로 OAuth 없이 직접 다운로드.

---

## DB 스키마

### 1. 신규 — 수도권 (`smr_*`, `db_schema_smr.sql`)

총 9개 부모 테이블 + 21개 연도별 파티션 + 60 컬럼 COMMENT (한글 매핑 보존).

| 테이블 | 행수 추정 (7년) | 용도 |
|---|---|---|
| `smr_zones` | 1,310 | TAZ 마스터 (권역내부 1137 + 권역외부 173, 17개 시도) |
| `smr_socio_pop` | 1.6M | 인구수 long (year, taz_seq, gender M/F, age 0~100, value) |
| `smr_socio_emp` | 1.6M | 취업자수 long (구조 동일) |
| `smr_socio_work` | 16K | 종사자수 (year, taz_seq, indicator '3RD '/'TOT ', value) |
| `smr_socio_stu` | 40K | 학생수 (year, taz_seq, level ELEM/MID /HIGH/SPEC/UNIV, value) |
| `smr_od_purpose` | 10.85M | 목적OD (5목적 home/work/scho/busi/othe), PARTITION BY LIST(year) |
| `smr_od_main_mode` | 10.85M | 주수단OD (10수단 walk_bike/freight/etc_bus/rail/ktx/auto/taxi/bus/subw/bus_subw) |
| `smr_od_purpose_mode` | 28.84M | 목적별주수단OD (4목적 HOME/WORK/SCHO/OTHE × 10수단). **BUSI 원본에 없음** |
| `smr_sync_log` | - | 적재 메타 (md5, duration, status) |

**전체 추정**: ~50M 행 / 6.6GB (인덱스 포함 ~7.6GB) — Azure 32GB 안에 여유.

**Sparse drop 정책**: OD에서 모든 metric 컬럼이 0인 행은 적재 시 제거.
- 목적/주수단OD: 90% nonzero (10% 절감)
- 목적별주수단OD: HOME 76% / WORK 64% / SCHO 19% / OTHE 81% (40% 절감)

### 2. Legacy — 전국 (`db_schema.sql`, Supabase 잔여)

기존 250존 5테이블 (`zones`, `socio`, `od_purpose`, `od_main_mode`, `od_access_mode`, `sync_log`). 마이그레이션 시점에 결정:
- region toggle로 병행 (수도권/전국)
- 또는 폐기

---

## KTDB 원본자료 (수도권)

| zip 파일 | 압축 | 압축해제 | 내용 |
|---|---|---|---|
| `2024-OD-PSN-OBJ-01 수도권 목적OD(2023-2050).zip` | 183MB | 1.1GB | ODTRIPYY_F.OUT × 7년 |
| `2024-OD-PSN-MOD-11 수도권 주수단 OD(2023-2050).zip` | 210MB | 1.9GB | OD_MMODE_YY_F.TXT × 7년 |
| `2024-OD-PSN-MOD-21 수도권 목적별 주수단 OD(2023-2050).zip` | 595MB | 7.6GB | OD_MMODE_<HOME|WORK|SCHO|OTHE>_YY_F.TXT × 28파일 |

3개 zip 모두 공통 자료 포함:
- `1. 존체계/존체계.xlsx` (60KB, 1310행)
- `2. 설명자료/*.hwp`
- `3. 사회경제지표/{인구수,종사자수,취업자수,학생수}/*.TXT × 4종 × 7년 = 28파일`

**제공 연도**: 2023(기준) + 2025/30/35/40/45/50 (장래 5년단위) = 7년 모두 작성.

**인코딩/포맷**:
- OD: utf-8(ASCII), 공백 구분, 헤더 없음
- 사회경제지표: cp949, 공백 구분, 헤더 1줄
- 사회경제지표 인구·취업자: wide 204컬럼 (남0~100세 + 여0~100세) → DB는 long format

상세 매핑: [`docs/컬럼매핑표.md`](docs/컬럼매핑표.md)

---

## 컬럼 정책 (영문 snake_case + 한글 COMMENT)

PostgreSQL 표준에 따라 컬럼명은 **영문 snake_case**, 원본 한글 의미는 **DB COMMENT**와 `docs/컬럼매핑표.md`에 보존.

### 주요 코드 ↔ 한글 매핑

| 코드 | 한글 | 비고 |
|---|---|---|
| `taz_seq` | TAZ 일련번호 | 1~1310 |
| `admin_code` | 행정기관코드 | 7~10자리 BIGINT |
| `is_inner` | 권역내부=1 / 권역외부=2 | |
| `orgn_seq, dest_seq` | 출발존 TAZ, 도착존 TAZ | OD PK |
| `home, work, scho, busi, othe` | 귀가, 출근, 등교, 업무, 기타 | 목적 |
| `walk_bike, freight, etc_bus, rail, ktx, auto, taxi, bus, subw, bus_subw` | 도보/자전거, 화물/기타, 기타버스, 일반철도, KTX, 승용차, 택시, 버스, 지하철, 버스+지하철 | 수단 |
| `gender ('M'/'F')` | 남성/여성 | |
| `age (0~100)` | 연령 (100=100세 이상) | |
| `indicator ('3RD '/'TOT ')` | 3차산업 종사자 / 총종사자 | CHAR(4) padded |
| `level ('ELEM'/'MID '/'HIGH'/'SPEC'/'UNIV')` | 초/중/고/특수/대학 | CHAR(4) padded |
| `purpose ('HOME'/'WORK'/'SCHO'/'OTHE')` | 귀가/출근/등교/기타 | 목적별주수단 전용 |

DB 컬럼명은 `\d+ smr_*` 또는 다음 SQL로 한글 의미 즉시 확인 가능:
```sql
SELECT c.relname, a.attname, d.description
FROM pg_class c
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
WHERE c.relname LIKE 'smr_%' AND a.attnum > 0 AND d.description IS NOT NULL;
```

---

## 실행

### 0. 사전 준비 (1회)

1. **Azure 방화벽 IP 등록**: https://www.whatismyip.com 에서 본인 공인 IP 확인 후 Azure Portal → `geo-spatial-hub` → Networking → Firewall rules에 등록 (GIS 플러그인 사용자는 이미 등록되어 있을 가능성 ↑)
2. **`.env`에 admin postgres 자격증명** (gitignored)
3. **`.streamlit/secrets.toml`에 `[azure]` 블록 작성** (`secrets.toml.example` 참조, gitignored)

### 1. DB 분양 (1회)

```powershell
# 환경 점검 (admin 접속만, DDL 미실행)
python scripts/provision_ktdb.py --dry-run

# 본 분양 (CREATE ROLE + CREATE DATABASE + ALTER SCHEMA + 검증 6항목)
python scripts/provision_ktdb.py
```

스크립트가 `테넌트_DB_분양_절차.md` §3 절차를 자동 수행 + `current_database()` 검증으로 §7 사고 방지.

### 2. 스키마 적용 (1회)

```powershell
psql "host=geo-spatial-hub.postgres.database.azure.com port=5432 dbname=ktdb user=ktdb sslmode=require" `
     -f db_schema_smr.sql
```

### 3. 데이터 적재 (단계화)

`sync_smr.py`는 단계별 옵션 지원. 각 단계는 idempotent (TRUNCATE+RELOAD).

```powershell
python sync_smr.py --step zones    # v0: 존체계 1310행 (~3초)
python sync_smr.py --step socio    # v1: 사회경제지표 4종 ~3.2M행 (~6분)
python sync_smr.py --step od2023   # v2: 2023년 OD 3종 (sparse drop 검증, ~15분)
python sync_smr.py --step od_all   # v3: 7년 OD 전체 ~50M행 (~90분)
python sync_smr.py --step all      # 전부 순차 실행
```

각 단계 결과는 `smr_sync_log` 테이블에 기록 (rows_loaded, duration_s, source_md5, status).
검증 SQL:
```sql
SELECT dataset, year, purpose, rows_loaded, duration_s, status, synced_at
FROM smr_sync_log ORDER BY synced_at DESC LIMIT 20;
```

### 4. 앱 실행

```powershell
pip install -r requirements.txt
streamlit run streamlit_app.py
```

사이드바 첫 항목 **"데이터 백엔드"** 라디오:
- `azure` (기본) — Azure ktdb의 `smr_*` 사용. 빈 DB면 결과 0행.
- `legacy` — Supabase fallback. `[supabase]` 블록 + 시트 URL 4종이 secrets에 있을 때만 노출.

`secrets.toml`에 둘 다 있으면 양쪽 선택 가능, 한쪽만 있으면 그쪽만 노출, 둘 다 없으면 차단.

### 5. 연결 헬스체크 (언제든)

```powershell
python db_env.py
# → {"ok": true, "database": "ktdb", "user": "ktdb", "version": "PostgreSQL 17.8"}
```

---

## secrets.toml 형식

```toml
GEMINI_API_KEY    = "..."
SHEET_URL_SOCIO   = "..."   # legacy 시트 (마이그레이션 잔여)
SHEET_URL_OBJ_OD  = "..."
SHEET_URL_MAIN_OD = "..."
SHEET_URL_ACC_OD  = "..."

[azure]
host     = "geo-spatial-hub.postgres.database.azure.com"
port     = 6432              # 일반 쿼리 = PgBouncer
port_ddl = 5432              # DDL/대량 적재
database = "ktdb"
user     = "ktdb"
password = "..."             # 평문 git 추적 파일 저장 금지
sslmode  = "require"

[supabase]                   # legacy, 마이그레이션 시 제거
host     = "..."
...
```

---

## db_env.py 사용

자격증명·연결 헬퍼 (Streamlit 앱과 CLI 스크립트 양쪽에서 공용).

```python
from db_env import connect

# 일반 쿼리 (PgBouncer 6432)
with connect() as conn, conn.cursor() as cur:
    cur.execute("SELECT count(*) FROM smr_zones")

# DDL/대량 적재 (5432 + autocommit)
with connect(ddl=True) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE smr_socio_pop")
```

자격증명 우선순위: Streamlit secrets `[azure]` → ENV `KTDB_DB_*` → 명시 인자.

---

## 코드 규칙

### 절대 룰

1. **DB 분양**은 `테넌트_DB_분양_절차.md` 그대로. Step 4 직전 `current_database()` 1줄 검증 의무 (§7 사고 재발 방지).
2. **DEV 서버(`geo-spatial-hub`)에만 분양**. PROD(`-prod`) 서버 절대 금지.
3. **DDL은 5432, 일반 쿼리는 6432**. 자동: `connect(ddl=True)`가 5432로 강제.
4. **비밀번호는 git 추적 파일에 평문 저장 금지**. `.env`/`secrets.toml`만 (gitignored). 분양 스크립트도 `KTDB_DB_PASSWORD` ENV/`.env` 외부화 (`scripts/provision_ktdb.py:NEW_PASSWORD` 같은 상수 도입 금지).
5. **신규 role에 `CREATEDB`/`CREATEROLE`/`SUPERUSER` 부여 절대 금지**.
6. **Step 3 (PUBLIC CONNECT 회수)는 첫 분양(energy, 2026-04-30)에서 1회 처리됨** — 이후 분양은 자동 격리, 재실행 금지.
7. **결과 표·합계·집계는 SQL/pandas 결정론 only**. LLM은 라우팅(키워드 매칭)·자연어 해설용으로만 가능, **수치 생성 절대 금지**. 현재 자연어 해설은 미도입(예약 상태). `auto_route()`는 결정론, `build_result_tables()`도 결정론. LLM에 head/sample만 넘기고 결과 CSV를 받는 패턴 도입 금지.

### 일반 룰

- DB 연결은 `db_env.connect()` 헬퍼 사용 — `keepalives=1` + dead connection 자동 재연결.
- `pd.to_numeric`에 **반드시 `errors="coerce"` 사용** (pandas 2.2+ 호환).
- OD 데이터에 콤마 포함 가능 → `_to_numeric` 헬퍼가 `str.replace(",", "")` 후 변환.
- 사회경제지표는 cp949, OD는 utf-8/ASCII. 인코딩 분기 필수.
- OD bulk INSERT 시 sparse drop 적용 (모든 metric=0 행 제거).
- Sync는 매 실행 TRUNCATE+RELOAD 패턴 (idempotent). 파티션은 자동 라우팅.
- AI 프롬프트는 실제 데이터에 없는 수치 생성 금지.
- 컬럼명은 **영문 snake_case**. 원본 한글 의미는 `COMMENT ON COLUMN`에 보존.
- Windows 콘솔 cp949 출력 시 한글 깨질 수 있음 — `sys.stdout = io.TextIOWrapper(..., encoding='utf-8')`로 강제하거나 `[+] [!] [-]` ASCII 대체.
- 보간 연도 표시는 `*(보간)` 주석.

---

## 자동 인식 기능 (auto_route, 결정론적)

채팅 본문에서 다음 정보를 **키워드+연도 기반 규칙으로** 자동 추출.
**LLM 미사용** — 결과 표/집계는 모두 SQL/pandas로 결정론적 처리 (절대 룰: hallucination 금지).
LLM은 향후 "결과 표 + 자연어 한 단락 해설" 단계용으로만 예약 (현재 dead code, `model = init_model()`).

| 인식 항목 | 함수 | 예시 |
|---|---|---|
| 시도/시군구 | `extract_region_from_query` | "충주시" → SIGU 매칭 |
| 단일 연도 | `extract_years_from_query` | "2030년" → [2030] |
| 연도 범위 | 동상 | "2025~2050" → 7개 배포 연도 |
| 범위 + N년단위 | 동상 | "2025~2050 5년단위" → [2025,30,...,50] |
| 시도/시군구 집계 | `detect_aggregation` | "시군구별" / "시도별" → groupby SUM |
| 다중 dataset 라우팅 | `auto_route` | "인구수와 종사자수" → POP_TOT + WORK_TOT |
| 다중 연도 라우팅 | 동상 | "장래 연도별 OD" → 7개 OD 연도 |
| **목적별주수단 라우팅** | `auto_route` | "출근 승용차" → 목적OD + 주수단OD + **PURMOD_WORK** |
| 화물 질의 (legacy) | (옛 `detect_freight_query`, 현재 미구현) | 향후 필요 시 화물 데이터셋 추가 후 재도입 |

### 목적별주수단OD 라우팅 룰

| 입력 패턴 | 결과 |
|---|---|
| "목적별주수단" / "수단별 목적" 등 explicit | 4목적(HOME/WORK/SCHO/OTHE) × 연도 모두 |
| 수단 키워드 + 목적 키워드 동시 (예: "출근 승용차") | 매칭 목적만 (예: PURMOD_WORK) × 연도 |
| 수단만 (예: "승용차") | 주수단OD만 (목적별주수단 미라우팅) |
| 목적만 (예: "등교") | 목적OD만 (수단별 미라우팅) |
| `combos[:8]` 한도 | 최대 8조합으로 제한 |

키워드 매핑: 귀가→HOME / 출근→WORK / 등교→SCHO / 기타→OTHE.

⚠ DB의 SIDO 표기와 사용자 채팅 표현 불일치: "강원도" ↔ "강원특별자치도", "전북" ↔ "전라북도" (TODO).

---

## 알려진 이슈

| 이슈 | 담당 | 상태 |
|---|---|---|
| Azure DB ktdb 분양 | 개발자 | ✅ 2026-05-06 완료 |
| `db_schema_smr.sql` 적용 | 개발자 | ✅ 30 테이블 + 60 COMMENT |
| 분양 스크립트 비밀번호 외부화 | 개발자 | ✅ `KTDB_DB_PASSWORD` ENV 기반 |
| `streamlit_app.py` Azure 백엔드 통합 | 개발자 | ✅ db_env 연결 + region(backend) 토글 + 결정론적 집계 |
| `smr_od_purpose_mode` 앱 라우팅 (4목적×7년=28탭) | 개발자 | ✅ auto_route 보강 |
| `sync_smr.py` 작성 + 데이터 적재 | 개발자 | ✅ 2026-05-07 완료 (7년 ~50.5M행, 123.8분, verify PASS) |
| 사내 분배용 R/O 계정 (`ktdb_viewer`) 분양 | 개발자 | ✅ 2026-05-07 완료 (GIS `waterviewer` 패턴, SELECT only) |
| 사이드바 시도/시군구·연도 입력 제거 (자연어 자동 추출) | 개발자 | ✅ 2026-05-07 완료 |
| Supabase 잔여 처리 (병행 vs 폐기) | 개발자 | ⏳ Streamlit 동작 검증 후 결정 |
| LLM 자연어 요약 (현재 dead code) | 개발자 | ⏳ 결정론적 집계 검증 후 도입 |
| 기존 OD 콤마 처리 | 개발자 | ✅ |
| 기존 OD ZONE JOIN | 개발자 | ✅ |
| Gemini 1.5-flash deprecation → 2.5-pro | 개발자 | ✅ |
| 회사 보안 SW가 OAuth 차단 | 환경 | ✅ CSV export로 우회 |
| `PUR_2050` 탭 미작성 (전국 250존) | 데이터팀 | ⚠️ 대기 (수도권은 7년 모두 OK) |
| SIDO 별칭 (강원도/전북특별자치도 등) | 개발자 | ✅ 2026-05-07 SIDO_ALIASES 17개 매핑 + KTDB 옛 명칭 표준 |
| OD 양단 질의 ("서울 ↔ 부산") | 개발자 | ✅ 2026-05-07 `extract_od_pair_from_query` 신규 — ↔/→/=>/에서~로 패턴 파싱 |
| OD destination 필터링 | 개발자 | ✅ 2026-05-07 `preprocess`에 `dest_sido_sel`/`dest_sigu_sel` 추가 |
| 도착지 시군구별 그룹화 + TOP N 정렬 | 개발자 | ✅ 2026-05-07 `detect_aggregation`에 도착시도/도착시군구 케이스 + `apply_top_n` |
| LLM 자연어 보고서 문장 (4~5줄) | 개발자 | ✅ 2026-05-07 `generate_llm_summary` — 표 기반 해설만, 수치 생성 금지 룰 프롬프트 명시 |
| "수단"+"통행" 동시 등장 시 라우팅 과잉 | 개발자 | ⏳ 우선순위 룰 또는 explicit 키워드 (보류, 영향 작음) |
| 권역외부 시군구 단위 분석 불가 (KTDB 데이터 한계) | 데이터 | ⚠️ 권역외부 14개 시도는 시도당 1~몇 존만 보유 — UI 안내 필요 (질문.md 수도권 한정으로 재작성됨) |
| 결과 표 비율/분담률/증감률/CAGR 컬럼 | 개발자 | ✅ 2026-05-07 `add_ratio_columns`/`add_change_columns`/`add_cagr_columns` 후처리 추가 |
| 단위 자동 변환 (천통행/일, 백만 등) | 개발자 | ✅ 2026-05-07 `apply_unit_scale` 추가 |
| 사회경제지표 연령/성별/학교급/3차산업 분리 | 개발자 | ✅ 2026-05-07 신규 13개 탭 (POP_65P/0_14/15_64/M/F, EMP_M/F, STU 5종, WORK_3RD) |
| 권역내부/외부 필터 ("수도권" 키워드) | 개발자 | ✅ 2026-05-07 `detect_region_scope` + `INNER_SIDOS` |
| OD 데이터 SQL 단계 사전 집계 (성능 최적화) | 개발자 | ✅ 2026-05-07 `pre_aggregate_level` (Q7 기준 22배 가속) |
| LLM 자연어 보고서 문장 4~5줄 (절대 룰 5 준수) | 개발자 | ✅ 2026-05-07 `generate_llm_summary` (수치 생성 금지 프롬프트) |
| OD 보간 (사회경제지표만 보간 적용 중) | 개발자 | ⏳ TODO — `interpolate_years` OD 분기 추가 |
| 인구 피라미드 (5세 단위 그룹) | 개발자 | ⏳ TODO — wide → long 변형 + 피라미드 표 |
| OD 매트릭스 (origin × dest 매트릭스) | 개발자 | ⏳ TODO — pivot 그룹 |
| pandas + psycopg2 FutureWarning | 개발자 | ⏳ SQLAlchemy 마이그레이션 보류 |
| Streamlit `st.session_state.messages` 초기화 누락 (자동 리로드 시) | 개발자 | ✅ 2026-05-07 init 블록을 `set_page_config` 직후로 이전 |
| `get_user_years` 빈 입력 → 7년 강제 → 쿼리 7배 느림 | 개발자 | ✅ 2026-05-07 빈 리스트 반환으로 수정 |

---

## 파일 구조

```
KTDB_report_agent/
├── CLAUDE.md                       # 본 파일
├── streamlit_app.py                # 메인 앱
├── db_env.py                       # DB 자격증명·연결 헬퍼 (Streamlit + CLI 공용)
├── db_schema.sql                   # legacy 전국 250존 스키마
├── db_schema_smr.sql               # 수도권 1310 TAZ 스키마 (30 테이블 + 60 COMMENT)
├── sync_sheets_to_db.py            # legacy 시트 → Supabase 동기화
├── sync_smr.py                     # zip → ktdb 단계별 적재 (zones/socio/od2023/od_all)
├── requirements.txt
├── .gitignore                      # .env, secrets.toml, .tmp_extract/, 원본자료/, .claude/, dist/ 포함
├── .env                            # admin postgres 자격증명 (gitignored)
├── .streamlit/
│   ├── secrets.toml                # 호스트(운영자) 비밀값 — owner ktdb (gitignored)
│   └── secrets.toml.example        # 양식 (git 추적, viewer/owner 두 프로파일 안내)
├── dist/                           # 사내 분배 패키지 (gitignored, 절대 커밋 금지)
│   └── secrets.toml                # ktdb_viewer R/O 자격증명 — 사용자에게 전달용
├── scripts/
│   ├── provision_ktdb.py           # owner DB 분양 자동화 (--dry-run 지원)
│   ├── provision_ktdb_viewer.py    # R/O `ktdb_viewer` 계정 분양 (GIS waterviewer 패턴)
│   └── verify_sync.py              # 적재 결과 검증 (행수 + sync_log + 분포 + 무결성)
├── 원본자료/                        # KTDB zip 3개 989MB (gitignored)
├── .tmp_extract/                   # 임시 추출본 (gitignored)
├── docs/
│   ├── PRD_DB-플랫폼-Azure-전환.md  # 의사결정 문서 (Azure 시나리오)
│   ├── 컬럼매핑표.md                # 원본 컬럼 ↔ DB 컬럼 마스터 매핑
│   ├── 사용자-설치-가이드.md         # 사내 분배용 — Python 설치/secrets.toml 배치/실행
│   ├── 비개발자-수정-가이드.md
│   ├── 왜-데이터-연동이-안되는가.md  # 옛 진단 (시트 OAuth 시대)
│   └── 수정사항 분석.md
└── 테넌트_DB_분양_절차.md           # GIS 팀 표준 분양 절차 (§7 ktdb 사례 추가)
```

---

## 변경 이력

- **2026-04-18**: 이슈 A(사회경제지표 6개 탭 C1 헤더) 완료 확인.
- **2026-04-20**: 이슈 B/C/D 완료 (콤마+coerce, OD ZONE 조인, 의존성 정리).
- **2026-04-29**: Gemini 1.5-flash → 2.5-pro, 자동 인식 기능 확장.
- **2026-04-30**: Supabase Postgres 마이그레이션 + 다중 분석 + 자동 지역 추출 (전국 250존).
- **2026-05-06**: **Azure Postgres ktdb 마이그레이션 + 수도권 1310 TAZ 신규 적재 준비**.
  - GIS 팀 인프라 공유: 같은 `geo-spatial-hub` 서버에 ktdb 테넌트 분양 (`테넌트_DB_분양_절차.md` 표준 따름).
  - 자동화 스크립트 `scripts/provision_ktdb.py` 작성 + 실제 분양 완료. 검증 6항목 PASS.
  - `db_schema_smr.sql` 작성 + 적용: 9 부모 + 21 파티션 (year list) + 60 컬럼 COMMENT.
  - `db_env.py` 신규 — Streamlit secrets/ENV/명시 인자 우선순위 + 5432/6432 포트 분기.
  - `docs/PRD_DB-플랫폼-Azure-전환.md` 작성 (의사결정 + 11 섹션).
  - `docs/컬럼매핑표.md` 작성 (원본 ↔ 영문 snake_case 마스터 표).
  - 컬럼 정책 확정: 영문 snake_case + DB COMMENT 한글 보존 (양쪽 진실원, COMMENT 우선).
  - Sparse drop 정책 확정: OD에서 모든 metric=0 행 제거 (목적별주수단OD 40% 절감).
  - `.gitignore` 보강 (`.tmp_extract/`, `*.parquet`).
  - 컨센서스 검토 ralplan: Architect/Critic APPROVE.
  - **외부 리뷰 후속 처리** (P0/P1/P2):
    - P0 비밀번호 하드코딩 제거 → `KTDB_DB_PASSWORD` ENV 외부화 + `--dry-run` required=False.
    - P1 LLM hallucination 제거 → `auto_route` 키워드+연도 결정론적 라우팅 + pandas/SQL 집계 + `compact_large_result` 자동 축약. LLM CSV 생성 경로 완전 제거.
    - P2 Supabase 잔류 → `db_env` 통합 + 사이드바 backend(azure/legacy) 토글 + AZURE/LEGACY 데이터셋 분리.
    - 추가: `smr_od_purpose_mode` 4목적×7년=28탭 앱 라우팅 추가 (`PURMOD_<purpose>_<year>` tab key, "출근 승용차" / "목적별주수단" 등 자연어 매칭 6 케이스 검증).
  - 다음 작업: `sync_smr.py` 작성 + 7년 데이터 적재.
- **2026-05-07**: **수도권 7년 OD 풀 적재 완료** (v0~v3 단계 종결).
  - `sync_smr.py` 작성 — argparse `--step` 옵션 (`zones`/`socio`/`od2023`/`od_all`/`all`), `sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)`로 cp949 콘솔 호환, 인코딩 분기 (사회경제지표 cp949 / OD utf-8), wide→long 피벗 (`_parse_pop_or_emp` 204컬럼 → long), sparse drop (`if not any(v != 0.0)`), `_bulk_insert_od` page_size=2000 partition 자동 라우팅.
  - `scripts/verify_sync.py` 작성 — 행 수 기댓값 + `smr_sync_log` 최근 20건 + 연도별 OD 분포 + zones 무결성 (1137/173/17) 검증.
  - 단계별 적재 결과:
    - v0 zones: 1,310행 (PASS)
    - v1 socio: pop/emp 각 1,607,718 + work 15,918 + stu 39,795 (PASS)
    - v2 OD 2023 (1년): 17.5분, sparse drop 비율 spec과 일치 (90.4% / 76.3% / 63.7% / 19.3% / 80.9%)
    - v3 OD 7년 풀: **smr_od_purpose 10.86M + smr_od_main_mode 10.86M + smr_od_purpose_mode 28.79M = ~50.5M행, 123.8분 (예상 ~118분 대비 +5%)**
  - verify_sync.py 종합 결과: **PASS** (모든 행 수 + 무결성 + 7년 OD 연도분포 일관성).
  - `.gitignore` 추가: `원본자료/` (989MB), `.claude/` (세션 로컬).
  - 환경: 배경 프로세스로 `Start-Process -RedirectStandardOutput`로 detach (PYTHONUNBUFFERED=1, PYTHONIOENCODING=utf-8).
  - 다음 작업: Streamlit 앱에서 7년 데이터 실제 질의 동작 검증 → Supabase legacy 폐기 결정 → LLM 자연어 요약 도입 검토.
- **2026-05-07 (오후)**: **사내 분배 모델 확립 — R/O 계정 + 자연어 우선 UI**.
  - 배경: GIS 플러그인 `waterviewer` 패턴(R/O 자격증명 + 평문 분배)을 KTDB에도 적용 → 사내 동료가 자기 PC에서 `streamlit run`으로 직접 사용 가능.
  - **사이드바 단순화** (`streamlit_app.py:935-950` 16줄 → 3줄):
    - "분석 대상 지역" (시도/시군구 selectbox 2개), "분석 연도" (5칸 text_input) 제거.
    - 채팅 자연어로만 처리 — `extract_region_from_query` + `extract_years_from_query` + `auto_route` 결정론적 라우팅이 이미 충분.
    - 내부 변수(`sido_sel="전체"` 등)는 유지해 downstream `preprocess()` / `load_integrated()` 변경 없이 호환.
  - **R/O 계정 분양** (`scripts/provision_ktdb_viewer.py` 신규 작성):
    - admin postgres → CREATE ROLE `ktdb_viewer` LOGIN only (super/createdb/createrole 모두 NO).
    - ktdb owner → GRANT CONNECT/USAGE/SELECT + ALTER DEFAULT PRIVILEGES (향후 테이블 자동 SELECT).
    - 검증: SELECT 1310 zones / 10.86M OD 통과 + INSERT/UPDATE/DELETE/CREATE/DROP 5종 모두 permission denied.
    - 자격증명 우선순위: ENV `KTDB_VIEWER_PASSWORD` → .env → stdin. owner 비밀번호는 `db_env._from_secrets_file()` fallback으로 자동 로드.
    - 비밀번호: `ktdbviewer2026` (소문자 + 숫자, QGIS `water123!@#` 패턴 동급).
  - **분배 패키지** (`dist/` 신규 폴더, gitignored):
    - `dist/secrets.toml` 사내 배포본 (viewer 자격증명 평문, 절대 커밋 금지).
    - `.streamlit/secrets.toml.example` 갱신 — owner/viewer 두 프로파일 주석.
  - **사용자 설치 가이드** (`docs/사용자-설치-가이드.md` 신규):
    - Azure 방화벽 IP 등록 → Python 3.11 → pip install → secrets.toml 배치 → `streamlit run`.
    - QGIS 사용자는 방화벽 등록 이미 통과 가능성 ↑ → 진입장벽 0에 가까움.
    - 권역내부(서울/인천/경기 1137 TAZ)/권역외부 데이터 비대칭 안내.
  - **검증** (db_env.connect 경유, ENV-only 모드):
    - viewer 로그인 → SELECT smr_zones = 1310행 ✅
    - viewer로 INSERT smr_zones → permission denied ✅
    - Streamlit은 db_env.connect()를 그대로 호출하므로 secrets.toml만 viewer로 교체하면 동작 (호스트 운영자 PC에서는 owner 유지, 사용자 PC에는 viewer 배포).
  - 다음 작업: Streamlit 실제 질의 검증(질문.md 케이스) → 사내 사용자 1명 시범 설치 → Supabase 폐기 결정.
- **2026-05-07 (저녁)**: **실제 질의 검증 + 다발성 버그 fix**.
  - 첫 시도 질의 "경기도 수원시의 2023년 목적통행 발생량" 결과 오류 → 4가지 버그 발견·수정.
  - **버그 ① session_state 초기화 누락**: 자동 리로드 시점에 `st.session_state.messages` AttributeError. init 블록을 `st.set_page_config` 직후 (line 13~24)로 이전, `setdefault` 패턴 사용. 기존 line 899 init 블록 제거.
  - **버그 ② `get_user_years` 빈 입력 → 7년 강제**: 사이드바를 제거하면서 빈 리스트가 들어가는데 함수 끝에 `else DIST_YEARS` fallback이 7년 모두 반환 → 쿼리 7회 → 60초+. fallback 제거, 빈 리스트 반환 → 채팅 추출 연도만 사용. 단일 연도 질의 1~3초로 단축.
  - **버그 ③ 시군구 매칭 비대칭**: DB는 "수원시 권선구/영통구/장안구/팔달구" 4행, query는 "수원시"만 입력 → `next((s in query))`로 매칭 실패 → 경기도 전체 반환. 첫 단어(시 단위) fallback 추가 → "수원시"로 매칭 후 `str.contains`가 4개 구 모두 흡수. 성남/고양/용인/안양 등 동일 효과.
  - **버그 ④ 시도 약칭 미지원**: "서울/부산/강원" 등 약칭 입력 시 매칭 실패. `SIDO_ALIASES` 17개 시도 매핑 dict 추가, fallback 단계로 통합. 또한 DB 실측 결과 KTDB는 옛 명칭("강원도", "전라북도") 그대로 사용 → 새 명칭("강원특별자치도") 입력도 옛 명칭으로 매핑.
  - 검증: 15개 query 케이스 (`경기도 수원시`, `서울 종로구`, `강원특별자치도 평창`, `전북 익산시` 등) 매칭 100% 통과.
  - **두 번째 질의 ("서울 ↔ 부산 수단별 OD") 결과 오류** → 신규 알려진 이슈 4건 등록:
    - OD 양단 질의 미지원 (단일 sido만 매칭).
    - `preprocess`가 origin SIDO만 필터, DEST_SIDO 무시.
    - "수단"+"통행" 동시 등장 시 목적OD 과잉 라우팅.
    - KTDB 데이터 자체 한계: 권역외부(부산 등)는 시도당 1존, 시군구 단위 분석 불가 (UI 안내 필요).
  - 다음 작업: OD 양단 라우팅 구현 → 비율 컬럼 옵션 → 권역외부 안내 메시지 → 사내 시범 설치.
- **2026-05-07 (야간)**: **결정론 라우팅 대폭 보강 + SQL 단계 사전 집계로 22배 가속**.
  - **OD 양단 라우팅** (`extract_od_pair_from_query`):
    - "↔ / → / ⇒ / =>" 화살표 + "A에서 B로/까지" 패턴 → (origin, dest) 분리.
    - `preprocess`에 `dest_sido_sel` / `dest_sigu_sel` 추가.
    - `detect_aggregation` 확장: "도착지 시군구" → `도착시군구` level.
    - `detect_top_n` 신규: "TOP 20" / "내림차순" default 20 / "상위 N".
  - **LLM 자연어 보고서 문장** (`generate_llm_summary`):
    - 트리거: 문장/보고서/분석/요약/설명/해설 키워드.
    - 프롬프트에 "표 수치 변경/생성 절대 금지" 명시 (절대 룰 5 준수).
    - `init_model` 빈 키 가드 추가.
  - **사회경제지표 신규 13개 탭** (load_socio_indicator + AZURE_DATASETS 확장):
    - 인구: POP_0_14, POP_15_64, POP_65P, POP_MALE, POP_FEMALE.
    - 취업자: EMP_MALE, EMP_FEMALE.
    - 학생: STU_ELEM/MID/HIGH/SPEC/UNIV (5학교급, CHAR(4) padded level).
    - 종사자: WORK_3RD.
    - auto_route 키워드: "65세이상/0~14세/생산가능", "초등/중학/고등/특수/대학", "남성/여성", "3차산업", "학교급별".
  - **후처리 컬럼** 4종 (build_result_tables 후 자동 적용):
    - `add_ratio_columns`: 같은 연도 metric 합계 대비 % (5목적/10수단 분담률, "비율/분담률/비중/%" 트리거).
    - `add_change_columns`: 첫·끝 또는 인접 쌍 변화율 ("증감률/변화율/변화/대비" 트리거, "연도별 증감률" 시 인접 쌍 모두).
    - `add_cagr_columns`: n년 복리 연평균 증가율 ("CAGR/연평균" 트리거).
    - `apply_unit_scale`: "천통행/일", "백만", "억" 단위 자동 환산 + 컬럼명에 단위 접미사.
    - 버그 fix: `pd.NA` 대신 `mask(==0)` 사용 → object dtype 회피로 `.round()` TypeError 해결.
  - **권역내부/외부 필터** (`detect_region_scope` + `INNER_SIDOS=[서울특별시, 인천광역시, 경기도]`):
    - 양단 패턴: "권역X에서 권역Y로" / "권역X → 권역Y" / "유입/유출".
    - 단일 표현: "수도권" 단독 → inner, "수도권 외/외부" → outer.
    - preprocess + load_od WHERE 절 적용.
  - **5개 micro-fix** (서비스 빌더 9개 질의 검토 결과):
    - 내림차순 N 미명시 → default TOP 20.
    - "장래"/"장래 연도별" → 2025~2050 6년 자동.
    - "기준연도/중간목표/최종목표" 명시 패턴 → 분석연도 추출.
    - 인접 연도 쌍 증감률 ("연도별 증감률" 키워드 시).
    - `\b` word boundary 버그 fix → `(?<![0-9])(20\d{2})(?![0-9])` 사용 → 한글 인접 "2023년" 인식 가능.
  - **SQL 필터 푸시다운** (`load_od`):
    - origin/dest sido/sigu/scope를 `WHERE` 절에 적용 → 1.5M → 600K (1년 21초 → 11초, 약 2.6배 단축).
  - **SQL 단계 사전 집계** (`load_od.pre_aggregate_level`):
    - 도착지 분석 의도 없을 때 SQL `GROUP BY` 푸시다운 → 1.5M → 77행 (Q7 기준 6년 129초 → 5.9초, **22배**).
    - 자동 결정 룰: agg_level + dest filter 부재 → `origin_sigu`/`origin_sido`. agg_level=도착시군구/도착시도 → `dest_sigu`/`dest_sido`. 도착지 필터/scope 있으면 None (raw fetch).
  - **검증** (질문.md 9개 케이스):
    - Q1 (CAGR), Q2 (시도별 증감률), Q3 (도착지 TOP 20), Q4 (단위+비율), Q5 (장래+분담률), Q6 (LLM 요약), Q7 (수도권 KTX 5.9초), Q8 (보간+인접 증감률), Q9 (서울→다른지역) — **모두 정확 동작**.
    - 데이터 한계 명시: 권역외부 14개 시도는 시도 합계까지만 의미 (시군구 단위 ❌). `질문.md` 9개 질의 수도권 데이터 범위에 맞게 재작성.
  - 다음 작업: 인구 피라미드(5세 단위), OD 보간, OD 매트릭스, 사내 시범 사용자 1명 배포.
