# KTDB 통합 분석 에이전트 — 사용자 설치 가이드 (간단판)

GIS 플러그인(`waterviewer` 패턴)과 동일한 R/O DB 접속 모델로 동작합니다.
각자 자기 PC에서 Streamlit을 띄워 사내망 Azure DB(`ktdb`)에 SELECT 쿼리만 보냅니다.

> **자격증명**: 분배본 `secrets.toml`에 들어 있는 계정은 `ktdb_viewer` (Read-Only).
> 누출되어도 데이터 변경/삭제 불가능 — QGIS 플러그인의 `waterviewer`와 같은 정책.

---

## 1. 사전 준비

### 1.1 Python 3.11 설치 (1회)

이미 Python이 깔려 있으면 `python --version`으로 확인. 3.11.x 권장.

미설치 시: https://www.python.org/downloads/ → 3.11.x 다운로드 → 설치 시 **"Add python.exe to PATH"** 체크.

---

## 2. 설치

### 2.1 코드 받기

운영자에게 받은 zip을 임의 폴더에 압축 해제하거나, git 접근권이 있으면 clone.

### 2.2 Python 패키지 설치

PowerShell 또는 명령 프롬프트에서 코드 폴더로 이동 후:

```powershell
pip install streamlit "google-generativeai>=0.8.0" psycopg2-binary pandas openpyxl
```

> 회사 보안 SW가 pip 외부 접근을 차단하면 사내 mirror/proxy 설정 필요. 운영자에게 문의.

### 2.3 자격증명 파일 배치

운영자가 전달한 `secrets.toml`을 다음 경로에 저장:

```
<코드폴더>/.streamlit/secrets.toml
```

> `.streamlit/secrets.toml.example`은 git에 포함된 양식. 실제 값은 운영자만 채워서 별도 전달.

### 2.4 (선택) 이메일 프롬프트 비활성화

Streamlit 첫 실행 시 이메일 입력을 묻습니다. 비활성화하려면 `%USERPROFILE%\.streamlit\credentials.toml`에:

```toml
[general]
email = ""
```

---

## 3. 실행

PowerShell 또는 cmd에서 코드 폴더로 이동 후:

```powershell
streamlit run streamlit_app.py
```

브라우저가 자동으로 `http://localhost:8501`을 엽니다.

> ⚠️ **흔한 실수**:
> - `streamlit run`이 아니라 **`python streamlit_app.py`** 로 실행하면 동작하지 않습니다 (브라우저 미오픈, ScriptRunContext warning 다수).
> - 파일 **더블클릭**도 동작하지 않습니다 — 반드시 터미널에서 위 명령 실행.
> - `streamlit: command not found` 또는 `'streamlit'은(는) 명령으로 인식되지 않습니다` 에러 시:
>   ```powershell
>   python -m streamlit run streamlit_app.py
>   ```

### 동작 확인

채팅창에 다음을 입력해보세요:

| 질의 예시 | 기대 결과 |
|---|---|
| `종로구 2030년 인구수` | 종로구 행만 표시, 2030년 컬럼 |
| `시군구별 인구수 2025` | 1,137개 권역내부 시군구 집계 |
| `2025~2050 5년단위 강남구 출근 승용차 OD` | 6개 연도 × 목적별주수단(WORK) 탭 |
