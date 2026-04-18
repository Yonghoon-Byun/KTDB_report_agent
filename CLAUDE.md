# KTDB 통합 분석 에이전트

## 프로젝트 개요

KTDB(국가교통DB) 데이터를 Google Sheets에서 읽어 Gemini AI로 자연어 분석하는 Streamlit 앱.

- **프론트엔드**: Streamlit (단일 파일 `streamlit_app.py`)
- **AI**: Google Gemini 1.5 Flash
- **데이터**: Google Sheets 4종 (gspread + 서비스 계정)
- **처리**: pandas

## Google Sheets 데이터 소스

| 데이터 | URL |
|--------|-----|
| 사회경제지표 | https://docs.google.com/spreadsheets/d/1pWLPhj2uz8auxsNIEuT2ovaD-xT7lzYkdgwN3i8Y4Wg/edit?usp=sharing |
| 목적OD | https://docs.google.com/spreadsheets/d/1du90sQtkdm5OyIx92XhmYAEhb_wpt07elm2jOSZP5Qk/edit?usp=sharing |
| 주수단OD | https://docs.google.com/spreadsheets/d/1E5tZKWv970J2soQ2n3K8jgz_RPgNPOpHXcuzhbBd3u0/edit?usp=sharing |
| 접근수단OD | https://docs.google.com/spreadsheets/d/1lHAuh2sHE2vcbNCW-eajBF60gqy4Yy6yy-zQOnD1uhQ/edit?usp=sharing |

## 시트 스키마 (확인된 실제 구조)

### 사회경제지표
- 탭: `ZONE`, `POP_TOT`, `POP_YNG`, `POP_15P`, `EMP`, `STU`, `WORK_TOT` (영어 코드)
- ZONE 탭 헤더: `SIDO, SIGU, ZONE`
- 나머지 6개 탭 헤더: `SIDO, SIGU, <빈칸>, 2023, 2025, 2030, 2035, 2040, 2045, 2050`
  - ⚠️ **C1 셀(3번째 컬럼 헤더)이 비어있음** — `ZONE`으로 입력 필요 (데이터팀 작업)

### 목적OD / 주수단OD
- 탭: `PUR_{연도}` / `MOD_{연도}` (연도: 2023, 2025, 2030, 2035, 2040, 2045, 2050)
- 목적OD 헤더: `ORGN, DEST, WORK, SCHO, BUSI, HOME, OTHE`
- 주수단OD 헤더: `ORGN, DEST, AUTO, OBUS, SUBW, RAIL, ERAI`
- 각 탭 62,499행
- ⚠️ **숫자에 콤마 포함** 문자열 (예: `"19,800"`)

### 접근수단OD
- 탭: `ATTMOD_2023` 단일
- 헤더: `ORGN, DEST, ATT_AANT, ATT_OBUS`

## 컬럼 코드 ↔ 한글 매핑

| 코드 | 한글 |
|------|------|
| SIDO, SIGU, ZONE | 시도, 시군구, 존번호 |
| ORGN, DEST | 발생존, 도착존 |
| WORK, SCHO, BUSI, HOME, OTHE | 출근, 등교, 업무, 귀가, 기타 |
| AUTO, OBUS, SUBW, RAIL, ERAI | 승용차, 버스, 지하철, 일반철도, 고속철도 |
| ATT_AANT, ATT_OBUS | 승용차(접근), 버스(접근) |

## 실행

### 로컬
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

`.streamlit/secrets.toml` 필요 (아래 참조).

### 배포
Streamlit Cloud: `share.streamlit.io` → Settings → Secrets에 아래 내용 입력.

## secrets.toml 형식

```toml
GEMINI_API_KEY = "..."
SHEET_URL_SOCIO   = "..."
SHEET_URL_OBJ_OD  = "..."
SHEET_URL_MAIN_OD = "..."
SHEET_URL_ACC_OD  = "..."

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@...iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
```

## 코드 규칙

- `pd.to_numeric`에 **반드시 `errors="coerce"` 사용** (`errors="ignore"`는 pandas 2.2+에서 제거됨 → `ValueError: invalid error value specified` 발생)
- OD 데이터는 콤마 포함 문자열 → `pd.to_numeric` 전에 `str.replace(",", "")` 필수
- 보간 연도는 `*(보간)` 주석 표기
- `secrets.toml`은 커밋 금지 (`.gitignore` 대상)
- AI 프롬프트는 실제 데이터에 없는 수치 생성 금지

## 알려진 이슈

진단 결과 상세: [`docs/왜-데이터-연동이-안되는가.md`](docs/왜-데이터-연동이-안되는가.md)

| 이슈 | 담당 | 상태 |
|------|------|------|
| 사회경제지표 6개 탭 C1 헤더 공백 (ZONE 헤더 누락) | 데이터팀 | 🔴 미수정 |
| `streamlit_app.py:275` `errors="ignore"` + 콤마 처리 부재 | 개발자 | 🔴 미수정 |
| OD 쿼리 지역 필터링용 ZONE 조인 미구현 | 개발자 | 🟡 추후 작업 |
| `requirements.txt`에 `gspread`/`google-auth` 명시 누락 | 개발자 | 🟡 배포 시 문제 |

상세 실행 계획: [`.omc/plans/diagnose-and-fix-sheets-connectivity.md`](.omc/plans/diagnose-and-fix-sheets-connectivity.md)

## 파일 구조

```
KTDB_report_agent/
├── CLAUDE.md              # 본 파일
├── streamlit_app.py       # 메인 앱
├── requirements.txt       # Python 의존성
├── .gitignore
└── docs/
    └── 왜-데이터-연동이-안되는가.md   # 비개발자용 진단 설명
```
