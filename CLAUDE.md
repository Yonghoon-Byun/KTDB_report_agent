# KTDB 통합 분석 에이전트

## 프로젝트 개요

KTDB(국가교통DB) 데이터를 **Supabase Postgres**에 적재한 뒤 Gemini AI로 자연어 분석하는 Streamlit 앱.
시트는 데이터팀의 입력 master, DB는 앱이 사용하는 읽기 전용 캐시.

- **프론트엔드**: Streamlit (단일 파일 `streamlit_app.py`)
- **AI**: Google Gemini 2.5 Pro
- **DB**: Supabase Postgres (NANO/Free, Seoul region, Session pooler)
- **데이터 소스**: Google Sheets 4종 (link-share CSV export로 anon 다운로드)
- **처리**: pandas + psycopg2

## 아키텍처

```
[Google Sheets master]            ← 데이터팀 입력
        ↓ sync_sheets_to_db.py    ← 사이드바 "🔄 데이터 동기화" 버튼 또는 CLI
[Supabase Postgres]               ← 5 테이블 (zones / socio / od_purpose / od_main_mode / od_access_mode)
        ↓ psycopg2 SQL JOIN       ← 사이드바/채팅에서 시도·시군구·연도 자동 매칭
[streamlit_app.py]
        ↓ Gemini 2.5 Pro          ← AI route 1~8개 (file, tab) 동시 분석
[표 + CSV 다운로드]
```

### 왜 Sheets에서 OAuth 안 쓰나
회사 보안 소프트웨어가 `oauth2.googleapis.com` 트래픽을 차단(WinError 10053). 시트는 link-share이므로 `gviz/tq?tqx=out:csv&sheet=<TAB>` URL로 OAuth 없이 직접 다운로드.

## DB 스키마 (`db_schema.sql`)

| 테이블 | 행 수 | 컬럼 | 용도 |
|---|---|---|---|
| `zones` | 250 | zone PK, sido, sigu | ZONE 마스터 |
| `socio` | 10,458 | indicator_code, zone, year, value (PK 3컬럼) | 사회경제지표 long format (POP_TOT/POP_YNG/POP_15P/EMP/STU/WORK_TOT × 7년) |
| `od_purpose` | 375,000 | year, orgn, dest, work, scho, busi, home, othe | 목적OD (2050 탭 미작성으로 6년) |
| `od_main_mode` | 437,500 | year, orgn, dest, auto, obus, subw, rail, erai | 주수단OD |
| `od_access_mode` | 62,500 | year=2023, orgn, dest, att_aant, att_obus | 접근수단OD |
| `sync_log` | - | table_name, rows_loaded, duration_s, synced_at | 동기화 메타 |

전체 ~88만 행 / 121MB (Free 500MB 중 24% 사용).

## Google Sheets 데이터 소스

| 데이터 | URL | 비고 |
|--------|-----|------|
| 사회경제지표 | https://docs.google.com/spreadsheets/d/1pWLPhj2uz8auxsNIEuT2ovaD-xT7lzYkdgwN3i8Y4Wg/edit?usp=sharing | C1 헤더(ZONE)는 시트에 빈 칸 → sync 스크립트가 3번째 컬럼 자동 매핑 |
| 목적OD | https://docs.google.com/spreadsheets/d/1du90sQtkdm5OyIx92XhmYAEhb_wpt07elm2jOSZP5Qk/edit?usp=sharing | PUR_2050 탭 미작성 (데이터팀 작업 대기) |
| 주수단OD | https://docs.google.com/spreadsheets/d/1E5tZKWv970J2soQ2n3K8jgz_RPgNPOpHXcuzhbBd3u0/edit?usp=sharing | 7개 연도 모두 작성 |
| 접근수단OD | https://docs.google.com/spreadsheets/d/1lHAuh2sHE2vcbNCW-eajBF60gqy4Yy6yy-zQOnD1uhQ/edit?usp=sharing | 2023 단일 |

## 컬럼 코드 ↔ 한글 매핑

| 코드 | 한글 |
|------|------|
| SIDO, SIGU, ZONE | 시도, 시군구, 존번호 |
| ORGN, DEST | 발생존, 도착존 |
| DEST_SIDO, DEST_SIGU | 도착시도, 도착시군구 (OD에서 ORGN→zones JOIN과 별도로 DEST→zones JOIN 결과) |
| WORK, SCHO, BUSI, HOME, OTHE | 출근, 등교, 업무, 귀가, 기타 |
| AUTO, OBUS, SUBW, RAIL, ERAI | 승용차, 버스, 지하철, 일반철도, 고속철도 |
| ATT_AANT, ATT_OBUS | 승용차(접근), 버스(접근) |

## 실행

### 로컬

```bash
# 의존성
pip install -r requirements.txt

# 첫 실행: DB 스키마 생성 (1회)
python -c "import psycopg2; conn = psycopg2.connect(...); conn.cursor().execute(open('db_schema.sql').read())"

# 첫 실행: 시트 → DB 동기화 (1~2분, 약 88만 행)
python sync_sheets_to_db.py

# 앱 실행
streamlit run streamlit_app.py
```

### 배포
Streamlit Cloud: `share.streamlit.io` → Settings → Secrets에 아래 내용 입력. 첫 배포 시 1회 sync 필요.

## secrets.toml 형식

```toml
GEMINI_API_KEY = "..."
SHEET_URL_SOCIO   = "..."
SHEET_URL_OBJ_OD  = "..."
SHEET_URL_MAIN_OD = "..."
SHEET_URL_ACC_OD  = "..."

[supabase]
host     = "aws-1-ap-northeast-2.pooler.supabase.com"
port     = 5432   # Session pooler (streamlit 같은 stateful 앱에 적합)
database = "postgres"
user     = "postgres.<project-ref>"
password = "..."
```

NANO 플랜은 `aws-1-*` cluster 사용 (구 free tier는 `aws-0-*`였음). port 6543(Transaction pooler)은 idle 끊김 잦아 5432(Session pooler) 권장.

## 코드 규칙

- DB 연결은 `init_db()` 헬퍼 사용 — `keepalives=1` + dead connection 자동 재연결.
- `pd.to_numeric`에 **반드시 `errors="coerce"` 사용** (pandas 2.2+ 호환).
- OD 데이터에 콤마 포함 가능 → `_to_numeric` 헬퍼가 `str.replace(",", "")` 후 변환.
- 보간 연도는 `*(보간)` 주석 표기.
- `secrets.toml`은 커밋 금지 (`.gitignore` 대상).
- AI 프롬프트는 실제 데이터에 없는 수치 생성 금지.
- Windows console에서 이모지(✓✗⚠️) 출력 시 cp949 에러 → ASCII 대체 (`[+] [!] [-]`).
- pandas `read_sql`에 psycopg2 connection 직접 전달 시 FutureWarning 발생 (동작은 OK). SQLAlchemy 마이그레이션은 추후.

## 자동 인식 기능

채팅 본문에서 다음 정보를 자동 추출하므로 사이드바 입력 없이도 질의 가능:

| 인식 항목 | 함수 | 예시 |
|---|---|---|
| 시도/시군구 | `extract_region_from_query` | "충주시" → SIGU 매칭, "충청북도 충주시" → 둘 다 |
| 단일 연도 | `extract_years_from_query` | "2030년" → [2030] |
| 연도 범위 | 동상 | "2025~2050" → 배포 연도 [2025, 2030, 2035, 2040, 2045, 2050] |
| 범위 + N년단위 | 동상 | "2025~2050 5년단위" → [2025, 2030, ..., 2050] |
| 다중 지표 라우팅 | `ai_route` | "인구수와 종사자수" → [(사회경제지표, POP_TOT), (사회경제지표, WORK_TOT)] |
| 다중 연도 라우팅 | 동상 | "장래 연도별" → 7개 PUR/MOD 탭 |
| 시도/시군구 집계 | `detect_aggregation` | "시군구별" / "시도별" 키워드 |
| 화물 질의 감지 | `detect_freight_query` | "화물" / "freight" / "물류" → 데이터 부재 안내 |

⚠️ DB의 SIDO 표기와 사용자 채팅 표현 불일치: "강원도" ↔ "강원특별자치도", "전북" ↔ "전라북도" (현재 alias 매핑 미구현 — TODO).

## 알려진 이슈

| 이슈 | 담당 | 상태 |
|------|------|------|
| 사회경제지표 6개 탭 C1 헤더 공백 | 데이터팀 | ⚠️ 시트 자체엔 미해결, sync 스크립트가 3번째 컬럼 자동 매핑으로 우회 |
| OD 데이터 콤마 처리 | 개발자 | ✅ |
| OD ZONE JOIN 미구현 (ORGN+DEST 양쪽) | 개발자 | ✅ |
| 의존성 정리 | 개발자 | ✅ |
| Gemini 1.5-flash deprecation | 개발자 | ✅ 2.5-pro로 교체 |
| 회사 보안 SW가 OAuth 차단 | 환경 | ✅ CSV export로 우회 |
| `PUR_2050` 탭 미작성 | 데이터팀 | ⚠️ 대기 (보간으로 임시 대응 가능) |
| SIDO 별칭 (강원도↔강원특별자치도 등) | 개발자 | ⏳ TODO |
| pandas + psycopg2 FutureWarning | 개발자 | ⏳ SQLAlchemy 마이그레이션 보류 |

## 파일 구조

```
KTDB_report_agent/
├── CLAUDE.md                       # 본 파일
├── streamlit_app.py                # 메인 앱 (DB 기반)
├── sync_sheets_to_db.py            # Sheets → Supabase 동기화 스크립트
├── db_schema.sql                   # DB 스키마 (5 테이블)
├── requirements.txt                # Python 의존성
├── .gitignore
├── .streamlit/secrets.toml         # 비밀값 (gitignored)
├── .venv-win/                      # Windows 네이티브 venv (gitignored)
└── docs/
    ├── 왜-데이터-연동이-안되는가.md     # 옛 진단 (시트 OAuth 시대)
    ├── 비개발자-수정-가이드.md          # 비개발자용 가이드
    └── 수정사항 분석.md                 # 옛 이슈 사양
```

## 변경 이력

- **2026-04-18**: 이슈 A(사회경제지표 6개 탭 C1 헤더) 완료 확인. 비개발자 수정 가이드 및 개발자 전달 문서 작성.
- **2026-04-20**: 이슈 B/C/D 완료.
  - B: `preprocess()`에 콤마 제거 + `pd.to_numeric(errors="coerce")`.
  - C: ORGN→ZONE 머지 로직 추가, OD 지역 필터링 동작.
  - D: `requirements.txt`에서 `st-gsheets-connection` 제거, `gspread>=5.0.0` + `google-auth>=2.0.0` 명시.
- **2026-04-29**: Gemini 모델 마이그레이션 + 질의 분석 기능 확장.
  - `gemini-1.5-flash` v1beta deprecation → `gemini-2.5-pro`로 교체.
  - `init_model()`에서 `genai.list_models()` 호출 제거 (첫 페이지 로딩 약 23초 단축).
  - 채팅 본문 연도 자동 추출, 시도/시군구별 집계, 화물 질의 감지/안내 추가.
  - CSV 다운로드 UTF-8 BOM literal prepend + charset 명시.
- **2026-04-30**: **Supabase Postgres 마이그레이션 + 다중 분석**.
  - 회사 보안 SW가 Google OAuth(`oauth2.googleapis.com`) 차단 → 시트 link-share CSV export로 anon 다운로드 우회.
  - 데이터를 Supabase Postgres(NANO, Seoul, Session pooler 5432)로 이전. 5개 테이블(`zones` / `socio` / `od_purpose` / `od_main_mode` / `od_access_mode`) + `sync_log`. 약 88만 행 / 121MB.
  - `sync_sheets_to_db.py` 신규 — `gviz/tq` CSV 다운로드 + `psycopg2.execute_values` 벌크 적재. 매 실행 TRUNCATE+RELOAD (idempotent). 전체 약 70초.
  - `streamlit_app.py` DB 기반으로 재작성. `gspread` 의존성 제거. `psycopg2-binary` 추가. `init_db()`에 keepalives + dead connection 자동 재연결.
  - 사이드바에 **"🔄 데이터 동기화" 버튼** 추가 (수동 시트→DB 갱신).
  - **OD 양방향 매핑**: `load_od()`가 ORGN→zones와 DEST→zones를 모두 JOIN. 결과에 `DEST_SIDO`/`DEST_SIGU` 컬럼 등장 (도착 도시명 추적 가능).
  - **연도 범위 인식**: `extract_years_from_query`가 "2025~2050", "5년단위", "10년단위" 패턴 인식.
  - **AI 다중 라우팅**: `ai_route`가 1~8개 (file, tab) 조합 반환. 여러 지표/연도를 한 번에 분석 가능 (예: "인구수와 종사자수 비교", "장래 연도별 수단OD").
  - **자동 지역 매칭**: 채팅에서 "춘천시", "충청북도" 등 키워드 추출 → SIDO/SIGU 자동 필터.
