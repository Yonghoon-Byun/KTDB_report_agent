# KTDB 통합 분석 에이전트

## 프로젝트 개요

KTDB(국가교통DB) 데이터를 Google Sheets에서 읽어와 Gemini AI로 자연어 분석하는 Streamlit 웹 애플리케이션.
사용자가 자연어로 질문하면 AI가 적합한 시트를 자동 선택하고, 데이터 분석 결과를 표+텍스트로 반환한다.

## 핵심 기능

1. **Google Sheets 데이터 연동**: gspread + 서비스 계정 인증으로 4종 시트 실시간 조회
2. **AI 시트 자동 선택**: 사용자 질문을 분석하여 적합한 시트/탭 자동 라우팅 (한글 설명 + 카테고리 힌트)
3. **자연어 분석**: Gemini 1.5 Flash로 질문 → 분석 요약 + CSV 표 생성
4. **연도 보간**: 배포 연도 외 입력 시 선형보간법 자동 적용
5. **지역 필터링**: 시도/시군구 단위 필터 + OD 데이터 ZONE 탭 조인
6. **대용량 집계**: OD 62,500행 데이터를 지역별 합산 후 AI에 전달
7. **CSV 다운로드**: 분석 결과를 CSV로 즉시 내려받기

---

## 빠른 시작

### 로컬 실행

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

`.streamlit/secrets.toml` 파일이 필요합니다 (아래 설정 참조).

### Streamlit Cloud 배포

1. https://share.streamlit.io 접속 → GitHub 로그인
2. **New app** 클릭:
   - Repository: `Yonghoon-Byun/KTDB_report_agent`
   - Branch: `main`
   - Main file: `streamlit_app.py`
3. **Advanced settings** → **Secrets** 탭에 아래 내용 입력 (secrets.toml 형식)
4. **Deploy** 클릭

---

## 설정 (secrets.toml)

로컬 실행 시 `.streamlit/secrets.toml`에, Streamlit Cloud 배포 시 Settings → Secrets에 입력.

```toml
GEMINI_API_KEY = "Gemini API 키"

SHEET_URL_SOCIO   = "https://docs.google.com/spreadsheets/d/..."
SHEET_URL_OBJ_OD  = "https://docs.google.com/spreadsheets/d/..."
SHEET_URL_MAIN_OD = "https://docs.google.com/spreadsheets/d/..."
SHEET_URL_ACC_OD  = "https://docs.google.com/spreadsheets/d/..."

[gcp_service_account]
type = "service_account"
project_id = "프로젝트ID"
private_key_id = "키ID"
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "xxx@xxx.iam.gserviceaccount.com"
client_id = "숫자ID"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
```

### 사전 준비

| 항목 | 취득 방법 |
|------|----------|
| Gemini API 키 | https://aistudio.google.com/apikey → Create API Key |
| GCP 서비스 계정 | Google Cloud Console → API 및 서비스 → 사용자 인증 정보 → 서비스 계정 만들기 → JSON 키 다운로드 |
| Google Sheets API | Google Cloud Console → API 라이브러리 → "Google Sheets API" + "Google Drive API" 활성화 |
| Sheets 공유 | 서비스 계정의 `client_email`을 Google Sheets 4종에 **편집자** 권한으로 공유 |

---

## 데이터 소스 (Google Sheets)

| 데이터 | Secrets 키 | 용도 |
|--------|-----------|------|
| 사회경제지표 | `SHEET_URL_SOCIO` | 인구, 취업자, 종사자 등 지역별 통계 |
| 목적OD | `SHEET_URL_OBJ_OD` | 출근/등교/업무/귀가/기타 통행량 |
| 주수단OD | `SHEET_URL_MAIN_OD` | 승용차/버스/지하철/철도 수단별 통행량 |
| 접근수단OD | `SHEET_URL_ACC_OD` | 접근수단별 통행량 |

### 시트별 탭 구성

| 시트 | 탭 코드 | 설명 | 컬럼 |
|------|---------|------|------|
| 사회경제지표 | ZONE | 존체계(행정구역) | SIDO, SIGU, ZONE |
| | POP_TOT | 총 인구수 | SIDO, SIGU, ZONE, 2023~2050 |
| | POP_YNG | 5-24세 인구수 | (동일) |
| | POP_15P | 15세이상 인구수 | (동일) |
| | EMP | 취업자수 | (동일) |
| | STU | 수용학생수 | (동일) |
| | WORK_TOT | 종사자수 | (동일) |
| 목적OD | PUR_{연도} | 목적OD (7탭) | ORGN, DEST, WORK, SCHO, BUSI, HOME, OTHE |
| 주수단OD | MOD_{연도} | 주수단OD (7탭) | ORGN, DEST, AUTO, OBUS, SUBW, RAIL, ERAI |
| 접근수단OD | ATTMOD_2023 | 접근수단OD (1탭) | ORGN, DEST, ATT_AANT, ATT_OBUS |

- 배포 연도: `2023, 2025, 2030, 2035, 2040, 2045, 2050`
- 단위: 사회경제지표 = 명, OD = 통행/일
- Google Sheets 스키마 상세: `docs/SHEETS_SCHEMA_GUIDE.md` 참조

---

## 시스템 아키텍처

```
[Google Sheets 4종]
       |
   [gspread + 서비스계정 인증]
       |
   [Streamlit App]
       |
   ┌───┴───┐
   |       |
[사이드바]  [채팅 UI]
 시도/시군구   질문 입력
 연도 선택       |
 시트 선택   [AI 라우팅] ← Gemini (시트 자동 선택)
       |       |
   [데이터 로드 + 전처리]
   · 콤마 제거 + 숫자 변환
   · ZONE 탭 조인 (OD→시도/시군구 매핑)
   · 지역 필터링
   · 연도 보간
   · 대용량 집계 (500행 초과 시)
       |
   [Gemini 분석]
   · SYSTEM_PROMPT 기반
   · 데이터 최대 250행 전달
       |
   [결과 출력]
   · 요약 텍스트 + CSV 표
   · CSV 다운로드 버튼
```

---

## 파일 구조

```
KTDB_report_agent/
├── CLAUDE.md              # 프로젝트 문서 (본 파일)
├── streamlit_app.py       # 메인 앱 (단일 파일)
├── requirements.txt       # Python 의존성
├── .gitignore
└── docs/
    └── SHEETS_SCHEMA_GUIDE.md  # DB 구축 담당자용 Google Sheets 스키마 가이드
```

## 기술 스택

- **언어**: Python 3.11+
- **프론트엔드**: Streamlit
- **AI/LLM**: Google Gemini 1.5 Flash (`google-generativeai`)
- **데이터**: Google Sheets (`gspread`, `google-auth`)
- **인증**: GCP 서비스 계정 (OAuth2)
- **데이터 처리**: pandas

---

## 컬럼 매핑 (영문 코드 → 한글)

| 코드 | 한글 | 분류 |
|------|------|------|
| SIDO | 시도 | 지역 |
| SIGU | 시군구 | 지역 |
| ZONE | 존번호 | 지역 |
| ORGN | 발생존 | OD |
| DEST | 도착존 | OD |
| 2023~2050 | 2023년~2050년 | 연도 |
| WORK | 출근 | 목적 |
| SCHO | 등교 | 목적 |
| BUSI | 업무 | 목적 |
| HOME | 귀가 | 목적 |
| OTHE | 기타 | 목적 |
| AUTO | 승용차 | 수단 |
| OBUS | 버스 | 수단 |
| SUBW | 지하철 | 수단 |
| RAIL | 일반철도 | 수단 |
| ERAI | 고속철도 | 수단 |
| ATT_AANT | 승용차(접근) | 접근수단 |
| ATT_OBUS | 버스(접근) | 접근수단 |

---

## 개발 규칙

- `secrets.toml`은 절대 커밋하지 않음 (`.gitignore` 대상)
- 데이터 단위(명, 통행/일)를 항상 표에 명시
- AI 프롬프트에서 실제 데이터에 없는 수치를 생성하지 않도록 제어
- 보간 연도는 `*(보간)` 주석 표기
- `pd.to_numeric`은 `errors="coerce"` 사용 (pandas 2.2+ 호환, `"ignore"` 사용 금지)

---

## 주요 처리 로직

### preprocess() — 전처리

1. 천단위 콤마 제거 (`"19,800"` → `19800`)
2. 숫자 변환 (`pd.to_numeric(errors="coerce")`)
3. OD 데이터인 경우 ZONE 탭 조인으로 ORGN → 시도/시군구 매핑
4. 시도/시군구 필터 적용
5. 존번호/발생존 기준 정렬
6. 영문 코드 → 한글 컬럼명 변환

### load_integrated() — 통합 로드

1. Google Sheets에서 데이터 로드
2. `preprocess()` 호출
3. 수치 컬럼 없으면 에러 (빈 데이터 방어)
4. 연도 보간 (선형보간법)
5. 500행 초과 시 시도/시군구별 집계

### ai_route() — AI 시트 자동 선택

- Gemini에 탭 코드 + 한글 설명 + 카테고리 힌트 전달
- 인구/종사자 → 사회경제지표, 출근/통행목적 → 목적OD, 승용차/수단 → 주수단OD

---

## 알려진 이슈 및 TODO

- **사회경제지표 데이터 업로드 필요** — Google Sheets의 POP_TOT~WORK_TOT 탭에 연도별 데이터를 채워야 함 (`docs/SHEETS_SCHEMA_GUIDE.md` 참조)
