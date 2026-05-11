# KTDB 통합 분석 에이전트 — 사용자 설치 가이드 (간단판)

GIS 플러그인(`waterviewer` 패턴)과 동일한 R/O DB 접속 모델로 동작합니다.
각자 자기 PC에서 Streamlit을 띄워 사내망 Azure DB(`ktdb`)에 SELECT 쿼리만 보냅니다.

> **자격증명**: 배포 zip에 포함된 `.streamlit/secrets.toml`의 계정은 `ktdb_viewer` (Read-Only).
> 누출되어도 데이터 변경/삭제 불가능 — QGIS 플러그인의 `waterviewer`와 같은 정책.

---

## 1. 사전 준비 (1회)

### Python 3.11 설치

이미 깔려 있으면 `python --version`으로 확인. 3.11.x 권장.

미설치 시: https://www.python.org/downloads/ → 3.11.x 다운로드 → 설치 시 **"Add python.exe to PATH"** 반드시 체크.

---

## 2. 설치 및 실행

### 2.1 zip 압축 해제

운영자에게 받은 `KTDB_분석에이전트.zip`을 임의 폴더(예: 바탕화면)에 압축 해제.

폴더 내용:
```
KTDB_분석에이전트/
├── KTDB실행.bat          ← 더블클릭으로 실행
├── streamlit_app.py
├── db_env.py
├── requirements.txt
├── .streamlit/
│   └── secrets.toml       ← R/O 자격증명 (포함됨)
└── docs/
    └── 사용자-설치-가이드-short.md
```

### 2.2 더블클릭으로 실행

`KTDB실행.bat` 더블클릭. 끝.

bat이 자동으로 처리하는 것:
1. Python 설치 여부 확인 (없으면 안내 후 종료)
2. 필요한 패키지(streamlit, tabulate, psycopg2, google-generativeai, openpyxl) 설치 여부 확인 → 누락된 게 있으면 **`pip install -r requirements.txt` 자동 실행** (첫 실행 시 1~2분 소요)
3. `python -m streamlit run streamlit_app.py` 기동
4. 브라우저가 자동으로 `http://localhost:8501`을 엶

> 회사 보안 SW가 pip 외부 접근을 차단하면 패키지 설치 단계에서 실패할 수 있습니다. 그럴 땐 사내 pip mirror/proxy 설정 필요 — 운영자에게 문의.

### 2.3 (선택) 이메일 프롬프트 비활성화

Streamlit 첫 실행 시 이메일 입력을 묻습니다. 비활성화하려면 `%USERPROFILE%\.streamlit\credentials.toml`에:

```toml
[general]
email = ""
```

---

## 3. 동작 확인

채팅창에 다음을 입력해보세요:

| 질의 예시 | 기대 결과 |
|---|---|
| `종로구 2030년 인구수` | 종로구 행만 표시, 2030년 컬럼 |
| `시군구별 인구수 2025` | 1,137개 권역내부 시군구 집계 |
| `2025~2050 5년단위 강남구 출근 승용차 OD` | 6개 연도 × 목적별주수단(WORK) 탭 |

---

## 4. 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| `Python이 설치되지 않았습니다` 메시지 | PATH 미등록 | 1번 단계 다시. 설치 시 "Add to PATH" 체크 확인 |
| `패키지 설치 실패` | 사내 보안 SW가 pip 차단 | 운영자에게 사내 mirror/proxy 설정 문의 |
| 한글이 깨져 나옴 (`'섎릺吏' is not recognized`) | 구버전 bat (UTF-8) | 운영자에게 새 zip 재요청 |
| `ImportError: Missing optional dependency 'tabulate'` | 구버전 설치본 + 패키지 누락 | PowerShell에서 `pip install tabulate` 한 줄 실행 또는 새 zip 재설치 |
| `ImportError: numpy.core.multiarray failed to import` (pyarrow 관련) | Anaconda Python의 conda numpy ↔ pip pyarrow ABI 충돌 | PowerShell에서 `pip install --upgrade --force-reinstall numpy pyarrow` 실행 후 콘솔 닫고 bat 다시 더블클릭 |
| 브라우저가 자동으로 안 열림 | Streamlit 첫 실행 또는 방화벽 | 콘솔 메시지의 `Local URL` 주소를 직접 브라우저 주소창에 붙여넣기 |
| 쿼리 결과가 0행 | 사내망 미연결 or Azure 방화벽 미등록 | VPN/사내망 확인. QGIS 플러그인 사용자는 통과 가능성 ↑ |

종료는 콘솔 창의 `Ctrl+C` 또는 창 닫기. 다음번에는 그냥 `KTDB실행.bat` 다시 더블클릭.
