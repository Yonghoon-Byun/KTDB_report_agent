# PRD — KTDB Report Agent DB 플랫폼 전환 (Supabase → Azure)

| 항목 | 내용 |
|---|---|
| **문서 ID** | PRD-2026-05-06-DB-PLATFORM |
| **작성일** | 2026-05-06 |
| **작성자** | 변용훈 |
| **상태** | 검토 대기 (Draft v1) |
| **대상 독자** | 비개발자 의사결정자, 인계받을 부서, IT 인프라 담당 |
| **목적** | Supabase에서 Azure Postgres로 DB를 옮길지, 그리고 그 과정에서 "API 경유 vs 직접 조회"를 어떻게 결정할지 합의 |

---

## 1. Executive Summary (한 페이지 요약)

**결론 한 줄**: Azure Database for PostgreSQL로 옮기되, **DB는 Streamlit이 직접 조회**(현재와 동일 방식)하고, **API 레이어는 지금 만들지 않는다**. 향후 다른 부서가 같은 DB를 자체 시스템에서 쓰겠다고 할 때 추가한다.

**3가지 핵심 변경**:
1. **DB 호스트**: Supabase → Azure Postgres (회사 테넌트 기준)
2. **용량**: 8GB(Supabase Pro) → **32GB(Azure)** → 데이터 분리(Parquet) 불필요로 단순화
3. **앱 호스팅**: Streamlit Cloud → **Azure App Service** 권장 (사내망 + 회사 보안 SW 우위)

**유지되는 것**: Streamlit 코드, 비개발자 수정 가이드, "🔄 데이터 동기화" 버튼, AI 자연어 질의.

---

## 2. 배경 — 왜 지금 이 결정을 다시 보는가

### 2.1 현재까지의 경위
- 다른 부서에서 Google Sheets + Streamlit으로 KTDB 보고 도구를 빠르게 프로토타입.
- Sheets 호출이 너무 느리고, 회사 보안 SW가 OAuth를 차단하여 link-share CSV로 우회 중.
- 본 프로젝트가 Supabase Postgres로 이전하여 속도/안정성 개선 (88만 행, 121MB / 500MB Free).
- **수도권 KTDB 추가 데이터** 분석 결과 7년 적재 추정 약 **6-12GB** → Supabase Free 500MB는 물론 Pro 8GB도 빠듯.

### 2.2 Azure 카드를 다시 꺼낸 이유
- 회사가 이미 Azure 구독을 보유할 가능성이 높음 → **개인 결제 부담 제거**.
- Azure Postgres는 **32GB부터 시작** → 용량 마진 충분 → 적재 전략이 단순해짐.
- 사내망/사내 보안 정책에 자연스럽게 통합 → OAuth 차단 같은 회사 보안 SW 충돌 회피.

### 2.3 본 PRD가 답해야 할 질문
1. Azure DB는 **"직접 조회"** 인가, **"API 호출"** 인가?
2. 기존 **Supabase 대비 장단점**이 무엇인가?
3. **Streamlit을 계속 쓸지, 다른 플랫폼이 더 적합한지**?
4. **비개발자 부서 인계** 시 운영 부담이 어떻게 달라지는가?

---

## 3. 핵심 개념 — "DB 직접 조회 vs API 호출"

비개발자가 가장 헷갈리는 부분이라 그림과 함께 설명합니다.

### 3.1 두 방식의 차이

```
[방식 A: 직접 조회]
   Streamlit 앱  ───SQL 쿼리───►  Azure Postgres
                ◄───데이터───
   * 앱이 DB에 직접 말 검 (현재 Supabase 방식과 동일)

[방식 B: API 경유]
   Streamlit 앱  ───HTTP 요청───►  API 서버  ───SQL───►  Azure Postgres
                ◄───JSON 응답───            ◄───데이터───
   * 앱이 DB에 직접 말 못 검, API 서버가 통역
```

### 3.2 어느 방식을 골라야 하는가

| 상황 | 권장 방식 | 이유 |
|---|---|---|
| Streamlit 앱 **1개만** 사용 | **직접 조회 (A)** | 빠르고 단순. API는 한 단계 더 느림 |
| Streamlit + **모바일/다른 시스템도** DB 공유 | API 경유 (B) | 여러 클라이언트에 통일된 규칙 |
| 다른 부서가 **자체 시스템**으로 같은 DB 쓰고 싶다 | API 경유 (B) | DB 비밀번호 공개 없이 권한 통제 |
| **현재 KTDB 시나리오** | **직접 조회 (A)** ⭐ | Streamlit 1개. API는 과잉 설계 |

### 3.3 추후 API가 필요하면?
Azure에서는 다음 중 하나를 30분~1일 내에 추가 가능 — **지금 만들 필요 없음**.
- **PostgREST 컨테이너**: Supabase가 내부적으로 쓰는 오픈소스. 자동 REST API.
- **Azure API Management**: 회사 표준 API 게이트웨이.
- **FastAPI + Azure App Service**: 직접 작성하는 가장 유연한 옵션.

---

## 4. 플랫폼 비교 — Supabase vs Azure Postgres

### 4.1 운영 측면 (비개발자 관점)

| 항목 | Supabase | Azure Postgres |
|---|---|---|
| 관리 화면 GUI | 매우 쉬움 (테이블/SQL/Storage 한 페이지) | Azure Portal — 더 복잡, IT 협조 필요 |
| 비밀번호/접속 정보 | `secrets.toml` 1개 | 동일 (호스트만 변경) |
| 자동 백업 | Free 7일 / Pro 30일 | 자동 35일 |
| 모니터링 | 간단한 그래프 | Azure Monitor — 강력하지만 학습 필요 |
| 요금 청구 | 개인 카드 / 회사 카드 | **회사 Azure 구독** (이미 있을 가능성) |
| 회사 보안 SW | 외부 클라우드 차단 위험 (현재 일부 발생) | **사내망이면 차단 우회** |
| 데이터 갱신 인터페이스 | Supabase 콘솔 또는 사이드바 버튼 | **사이드바 "🔄 동기화" 버튼** |

### 4.2 기술 측면

| 항목 | Supabase Free | Supabase Pro | Azure Postgres B1ms (권장) |
|---|---|---|---|
| 용량 | 500MB | 8GB | **32GB** (확장 가능) |
| 비용 | $0 | $25/월 | ~$15/월 (또는 회사 결제) |
| 7년 KTDB 데이터 | **불가능** | 빠듯 (Parquet 분리 필요) | **여유** (단일 DB) |
| 적재 전략 | - | 혼합 (DB + Parquet) | **전체 Postgres** (단순) |
| 분산 일관성 메커니즘 | - | 2-phase commit + md5 verify | **불필요** |
| 회사망 통합 | 외부 | 외부 | **사내망** |

### 4.3 결론
**Azure Postgres B1ms 32GB가 KTDB 시나리오에 가장 적합**. 비용은 Supabase Pro 대비 비슷하거나 저렴, 용량 마진은 4배, 회사 보안 정책과 자연스러움.

---

## 5. 웹 플랫폼 비교 — Streamlit을 계속 쓸 것인가

### 5.1 후보 비교 (AI 채팅 + 표/차트 + 비개발자 운영성 기준)

| 플랫폼 | AI 채팅 UI | 차트/표 | 비개발자 수정 | 회사망 호스팅 | 비용 |
|---|---|---|---|---|---|
| **Streamlit** ⭐ | 쉬움 (`st.chat_input`) | 보통 | **단일 .py 파일** | Streamlit Cloud / Azure App Service | 무료~ |
| Gradio | 쉬움 | 약함 | 중간 | 가능 | 무료 |
| Power BI | **약함** (자연어 Q&A 한정) | **매우 강함** | GUI 쉬움 | 회사 표준 가능 | 라이선스 필요 |
| Next.js + Vercel | 자유롭게 (개발 필요) | 강함 | **개발자 필요** | Azure 가능 | 무료~ |
| Dash (Plotly) | 가능 (구현 필요) | 강함 | 중간 | 가능 | 무료 |
| **Azure App Service에 Streamlit** | 동일 | 동일 | 동일 | **회사망 OK** | B1 ~$13/월 |

### 5.2 권장 — Streamlit 유지, 호스팅만 Azure App Service로

이유:
1. **AI 채팅 + 표 + CSV 다운로드 조합에 Streamlit이 가장 적합**
2. 다른 부서도 Streamlit 익숙 → 인계 부담 최소
3. 단일 `.py` 파일 → 비개발자 수정 가이드 이미 있음 (`docs/비개발자-수정-가이드.md`)
4. Power BI로 가면 **AI 자연어 질의가 약화** → KTDB 핵심 가치 훼손
5. 코드 변경 없이 **호스팅만 사내망으로** 옮겨 회사 보안 정책 만족

---

## 6. 권장 아키텍처 (Azure 시나리오)

### 6.1 전체 흐름

```
[데이터 소스]
   원본 KTDB zip (수동 다운로드)
        │
        ▼
[적재 도구 — 분기 1회 등 부정기]
   sync_smr.py (Python 스크립트)
        │
        ▼
[저장소 — 회사 Azure]
   Azure Database for PostgreSQL Flexible Server
   - 32GB Burstable B1ms (~$15/월 또는 회사 결제)
   - 사내망 + SSL 강제
        │
        ▼
[웹 앱 — Streamlit]
   streamlit_app.py
   - 기존 코드 99% 그대로
   - host 한 줄만 Azure로 교체
        │
        ▼
[호스팅]
   Azure App Service B1 (~$13/월)
   - 사내망, 회사 보안 SW와 충돌 없음
   - 외부 사용자 접근은 회사 VPN 또는 IP 화이트리스트
```

### 6.2 데이터 적재 단순화

수도권 KTDB 7년 전체 약 **6.6GB** + 인덱스/오버헤드 약 **7.6GB** → Azure 32GB 안에 **여유롭게** 들어감.

| 데이터셋 | 7년 행 수 | 추정 용량 |
|---|---|---|
| `smr_zones` | 1,310 | < 1MB |
| `smr_socio_*` (인구/취업자/종사자/학생, long format) | 3.2M | ~100MB |
| `smr_od_purpose` (목적OD) | 10.85M | ~850MB |
| `smr_od_main_mode` (주수단OD) | 10.85M | ~1.4GB |
| `smr_od_purpose_mode` (목적별주수단OD) | 28.84M | ~4.2GB |
| 합계 | 약 53M | **~6.6GB** |

→ Supabase Pro 시나리오의 **Parquet 분리 / 2-phase commit / md5 verify 모두 제거 가능** → 운영 복잡도 감소.

---

## 7. 마이그레이션 영향 — 무엇이 바뀌는가

| 영역 | 현재 (Supabase) | 변경 후 (Azure) |
|---|---|---|
| `secrets.toml` | `host = "aws-1-...pooler.supabase.com"` | `host = "ktdb.postgres.database.azure.com"` + `sslmode = "require"` |
| `streamlit_app.py` | 변경 없음 | **변경 없음** ✓ |
| `sync_sheets_to_db.py` | Supabase 적재 | 동일 (호스트만) |
| `sync_smr.py` (예정) | Supabase 적재 | 동일 (호스트만) |
| 용량 한계 | Pro 8GB 마진 부족 | **32GB 여유** |
| 적재 전략 | Plan v2 A''(혼합) | **Plan v3 A'(전체 Postgres)** |
| Parquet 외부 호스팅 | 필요 | **불필요** |
| 2-phase commit | 필요 | **불필요** |
| 비개발자 수정 가이드 | 그대로 | 그대로 |

**코드 변경 분량**: 한 파일의 **DB 호스트 한 줄**. 비즈니스 로직 0줄.

---

## 8. 의사결정 체크리스트 — 진행 전 확인할 것

비개발자 + IT 부서 협업으로 답해야 함:

1. **회사 Azure 구독 보유 여부** — 보통 IT부서/정보화팀이 답함.
2. **Azure Database for PostgreSQL Flexible Server 생성 권한** — 없으면 IT 협조 필요.
3. **Streamlit 호스팅 위치** — 사내망(Azure App Service)인지, 외부(Streamlit Cloud) 유지인지.
4. **다른 부서 인계 시점** — 지금이면 API 같이 설계, 나중이면 보류.
5. **데이터 갱신 주체** — 본인이 직접 sync 스크립트 실행할지, 인계 부서가 사이드바 버튼만 누를지.

이 5개에 답이 나와야 Plan v3 확정 가능.

---

## 9. 리스크 및 완화책

| # | 리스크 | 영향 | 완화책 |
|---|---|---|---|
| 1 | 회사 Azure 구독 없음 | 진행 불가 | Supabase Pro로 회귀 (Plan v2 A'' 적용) |
| 2 | Azure Postgres 생성 권한 없음 | 진행 지연 | IT 부서 작업 요청서 사전 제출 |
| 3 | 회사 보안 SW가 외부 → Azure 트래픽도 차단 | Streamlit Cloud 사용 시 영향 | App Service로 사내망 호스팅 전환 |
| 4 | 비개발자 부서가 Azure Portal 어려워함 | 운영 부담 ↑ | Portal 직접 조작 불요. **사이드바 동기화 버튼**이 인터페이스 |
| 5 | 다른 부서가 자체 시스템에서 DB 직접 접근 요구 | 권한/보안 이슈 | API 레이어 추가 (PostgREST 또는 FastAPI) |
| 6 | 데이터 갱신 시 zone admin_code 변경 | JOIN 깨짐 | sync 스크립트에 `SyncIntegrityError` 검출 + manual approval |

---

## 10. 다음 단계 (단계화 실행 계획)

### Phase A — 의사결정 (1주)
- [ ] 9장 체크리스트 5개 항목 확인
- [ ] IT 부서에 Azure Postgres 생성 가능 여부 문의
- [ ] 본 PRD 검토 + 승인

### Phase B — 인프라 구축 (2-3일)
- [ ] Azure Postgres Flexible Server 생성 (B1ms 32GB)
- [ ] 방화벽 규칙 설정 (Streamlit 호스팅 IP 또는 사내망 only)
- [ ] SSL 인증서 확인
- [ ] `secrets.toml` 업데이트

### Phase C — 데이터 적재 (반나절)
- [ ] `db_schema_smr.sql` 작성 + 적용
- [ ] `sync_smr.py` 작성 (zip → 파싱 → 정규화 → bulk INSERT)
- [ ] 검증: 1310 zones, 1.72M OD/년, 1137 socio/년

### Phase D — 앱 통합 (반나절)
- [ ] `streamlit_app.py`에 region toggle (전국 250존 / 수도권 1310존)
- [ ] AI route에 region 파라미터 추가, "수도권" 키워드 자동 감지
- [ ] E2E 검증 쿼리 ("2023 수도권 인구", "서울→경기 출근 통행")

### Phase E — 운영 인계 (1일)
- [ ] `docs/비개발자-수정-가이드.md` 업데이트 (Azure 호스트 정보)
- [ ] 인계 부서 데모 + 사이드바 버튼 사용법 시연
- [ ] 백업/복구 SOP 작성

---

## 11. 부록 — 자주 묻는 질문

**Q1. API를 처음부터 안 만들면 나중에 후회하지 않을까?**
A. 후회 안 합니다. Postgres 위에 PostgREST 컨테이너 1개만 띄우면 자동 REST API가 생깁니다. 30분~1일 작업. 미리 만들면 유지보수 부담만 늘어남.

**Q2. Supabase의 장점인 자동 인증/권한 기능은 어떻게 되나?**
A. 현재 KTDB 앱은 단일 사용자 시나리오라 Supabase의 인증 기능을 사용하지 않고 있습니다. Azure에서도 마찬가지. 향후 멀티유저면 Azure AD B2C 또는 PostgREST + JWT 추가.

**Q3. Streamlit이 느릴 텐데 다른 플랫폼이 더 빠르지 않나?**
A. 느린 원인은 **DB 호출 속도**이지 Streamlit 자체가 아닙니다. Sheets → Postgres로 이미 100배 이상 빨라졌고, Azure로 가면 비슷한 수준 유지. UI 라이브러리를 바꿔도 DB가 같으면 체감 차이 거의 없음.

**Q4. 데이터 갱신을 비개발자가 어떻게 하나?**
A. 사이드바의 "🔄 데이터 동기화" 버튼이 그 인터페이스입니다. 새 KTDB zip 파일을 정해진 폴더에 두고 버튼 누르면 자동 적재. Azure Portal에 직접 들어갈 필요 없음.

**Q5. 회사 Azure 구독이 없으면?**
A. Plan v2 (Supabase Pro 8GB + Parquet 혼합)로 회귀. 비용은 비슷하지만 운영 복잡도가 더 높음. Azure 구독 확보가 우선.

---

## 변경 이력
- **2026-05-06 v1**: 초안 작성. Azure Postgres 전환 권장, Streamlit 유지, API 보류.
