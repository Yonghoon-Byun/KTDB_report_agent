# 테넌트 DB 분양 절차

> **작성일**: 2026-04-30
> **대상 독자**: GIS DB 관리자 (인수자 포함)
> **목적**: 같은 Azure PostgreSQL Flexible Server에 **다른 팀 전용 DB**를 만들고 계정·권한·격리·전달까지 표준 절차로 처리한다.
> **첫 적용 사례**: `energy` DB (2026-04-30 신설)

---

## 1. 언제 이 절차를 쓰는가

같은 서버 인프라에서 **타 팀에게 DB를 분양**해야 할 때.

- 인프라 비용 절감 (서버 1대로 다수 팀 수용)
- 기존 dde-water (개발 DB) 무수정 보호 — **CLAUDE.md 절대 룰 1번 적용**
- 신규 DB는 **PROD가 아닌 DEV 서버**(`geo-spatial-hub`)에 분양 (PROD는 운영 전용)

> **PROD 서버에 분양 금지** — `geo-spatial-hub-prod`는 GIS 운영 워크로드 전용.
> 분양은 반드시 DEV 서버(`geo-spatial-hub`)에 한다.

---

## 2. 격리 모델 — 무엇이 어떻게 분리되는가

| 격리 항목 | 방법 | 효과 |
|---|---|---|
| **DB 단위 격리** | 신규 DB 별도 생성, owner = 신규 role | 다른 팀 데이터·테이블 절대 안 섞임 |
| **PUBLIC 권한 회수** | dde-water·postgres에서 `REVOKE CONNECT FROM PUBLIC` | 신규 role이 다른 DB에 들어가지 못함 |
| **Role 속성 제한** | `CREATE ROLE`만 사용, `CREATEDB/CREATEROLE/SUPERUSER` 미부여 | 신규 role은 자기 DB 안에서만 owner |
| **PgBouncer 분리 미적용** | 같은 6432 공유 | Azure Flex 단일 PgBouncer 운영, 충분한 격리 |

### 격리되는 것 vs 안 되는 것

✅ **격리됨**:
- 다른 DB의 테이블·데이터 조회/수정 불가
- 다른 DB로 로그인 시도 시 `permission denied` 즉시 차단
- 신규 role은 superuser/createdb/createrole 권한 없음

⚠️ **격리 안 됨 (PostgreSQL 한계)**:
- `pg_database`, `pg_roles` 시스템 카탈로그는 누구나 조회 가능 → DB 이름·role 이름은 보임 (단, 비밀번호·데이터는 안 보임)
- 즉, 다른 팀이 `dde-water`라는 DB가 존재한다는 사실은 알 수 있지만 **접근은 불가**

---

## 3. 실행 절차 (재현 가능)

### 3.1 사전 준비

| 항목 | 값/확인 |
|---|---|
| 접속 도구 | DBeaver 또는 `psycopg2` (Python) |
| 관리 계정 | `postgres` (관리자 비밀번호는 `.env` 참조) |
| **포트** | **5432 (직접 연결)** ← DDL은 PgBouncer 6432 사용 금지 |
| SSL | 필수 (`sslmode=require`) |
| Azure 방화벽 | 작업자 본인 IP 등록 필요 |

> **CLAUDE.md 절대 룰 3번**: DDL/대량 작업은 5432, 일반 쿼리·플러그인은 6432.

### 3.2 SQL 절차 (관리자 계정, port 5432)

#### Step 1 — 신규 role 생성
```sql
CREATE ROLE <팀명> WITH LOGIN PASSWORD '<별도 정의>';
-- 추가 속성 부여 금지 (createdb/createrole/superuser 절대 X)
```

#### Step 2 — 신규 DB 생성 (소유자 = 신규 role)
```sql
CREATE DATABASE <팀명> OWNER <팀명>;
```
> 트랜잭션 블록 안에서 실행 불가 → autocommit 모드 또는 별도 명령으로 실행.

#### Step 3 — dde-water·postgres에서 PUBLIC CONNECT 회수
```sql
REVOKE CONNECT, TEMPORARY ON DATABASE "dde-water" FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
```

> **사전 확인**: `dde-water`의 ACL에 기존 사용자(`postgres`, `water`, `waterviewer` 등)가 **명시적 grant**(`postgres=CTc/postgres`, `water=c/postgres` …)를 가지고 있어야 한다. 없으면 회수 후 그 계정이 끊긴다.
>
> 확인 SQL:
> ```sql
> SELECT datname, datacl::text FROM pg_database
> WHERE datname IN ('dde-water','postgres');
> ```

#### Step 4 — 신규 DB 안에서 public schema 권한 정리

신규 DB에 접속을 바꾼 뒤 (`\c <팀명>` 또는 별도 connection):
```sql
ALTER SCHEMA public OWNER TO <팀명>;
GRANT ALL ON SCHEMA public TO <팀명>;
```
(PG 15+ 기본값 보정 — public schema의 CREATE 권한이 PUBLIC에서 회수된 상태이므로 명시 grant 필요.)

> ⚠ **컨텍스트 사고 주의 — 실행 전 반드시 확인**
>
> Step 4는 신규 DB에 **접속을 바꾼 뒤** 실행해야 한다. dde-water 또는 다른 DB에 연결된 채로 실행하면 그 DB의 schema OWNER가 신규 role로 변경되어 기존 사용자(`water`·`waterviewer` 등)가 schema USAGE를 잃는다.
>
> 실행 직전 검증 1줄:
> ```sql
> SELECT current_database(); -- 반드시 <팀명> 출력
> ```
> 다른 값이면 즉시 중단. 본 사고 사례: §7 "사고 이력" 참조.

### 3.3 Python 자동화 스크립트 패턴

`scripts/common/db_connection.py`의 `psycopg2.connect(...)` 패턴을 사용하되 **port=5432, autocommit=True**로 호출. DDL을 한 트랜잭션 밖에서 실행해야 `CREATE DATABASE`가 통과한다.

```python
conn = psycopg2.connect(
    host="geo-spatial-hub.postgres.database.azure.com",
    port=5432,                    # ← DDL은 5432
    user="postgres", password=...,
    dbname="postgres",
    sslmode="require"
)
conn.autocommit = True            # ← CREATE DATABASE 필수
```

이번 energy 케이스의 실제 실행 로그는 본 문서 §6 검증 결과를 참조.

---

## 4. 검증 절차

신규 DB 분양 후 **반드시** 6항목 모두 확인.

| # | 항목 | 기대값 |
|:-:|---|---|
| 1 | 신규 role 속성 | `super=F, createdb=F, createrole=F, replication=F, bypassrls=F` |
| 2 | dde-water·postgres ACL | `=Tc/postgres` (PUBLIC) 항목 **사라짐** |
| 3 | 신규 role → 신규 DB 로그인 | OK |
| 4 | 신규 role → dde-water 로그인 시도 | FATAL: permission denied |
| 5 | 신규 role → postgres 로그인 시도 | FATAL: permission denied |
| 6 | 신규 DB 안에서 CREATE/INSERT/SELECT/DROP | 모두 OK |

### 검증 SQL 헬퍼

```sql
-- ACL 확인
SELECT datname, datacl::text FROM pg_database
WHERE datname IN ('dde-water','postgres','<팀명>');

-- role 속성 확인
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole,
       rolcanlogin, rolreplication, rolbypassrls
FROM pg_roles WHERE rolname='<팀명>';

-- 권한 매트릭스 확인 (관리자 계정에서)
SELECT has_database_privilege('<팀명>','dde-water','CONNECT') AS can_dde,
       has_database_privilege('<팀명>','postgres','CONNECT') AS can_pg,
       has_database_privilege('<팀명>','<팀명>','CONNECT')   AS can_self;
-- 기대: can_dde=false, can_pg=false, can_self=true
```

---

## 5. 인수 팀에게 전달할 안내문 (양식)

비밀번호는 **본 문서가 아닌 별도 채널**(Slack DM, 이메일 등)로 전달.

```text
[<팀명> DB 접속 정보]
서버:   geo-spatial-hub.postgres.database.azure.com
포트:   6432
DB:     <팀명>
ID:     <팀명>
PW:     (별도 전달)
SSL:    필수 (sslmode=require)

[DBeaver 설정]
1) Database → New Database Connection → PostgreSQL
2) 위 정보 입력
3) SSL 탭 → "Use SSL" 체크 → sslmode = require
4) Test Connection → 성공 확인

[권한]
- 본인 DB 안에서 테이블/스키마/인덱스 자유롭게 생성·적재·관리 가능
- public 스키마 owner는 본인 role
- 다른 DB(dde-water 등)는 보이지도 접근되지도 않음

[주의]
- 사용 시작 전에 본인 공인 IP를 관리자에게 알려주세요 (Azure 방화벽 등록 필요)
  공인 IP 확인: https://www.whatismyip.com
- 비밀번호 변경 원하면 관리자에게 요청
- DDL 대량 작업 시에만 5432 포트 사용 (일반 쿼리는 6432 권장)
```

---

## 6. Azure 방화벽 등록 절차 (관리자 직접)

각 팀원의 공인 IP를 받은 후:

1. Azure Portal → `geo-spatial-hub` 서버 선택
2. 좌측 메뉴 **Networking** → **Firewall rules**
3. **+ Add a firewall rule** 클릭
4. 입력:
   - Rule name: `<팀명>_<사용자>_사무실` 등 식별 가능한 이름
   - Start IP / End IP: 동일한 공인 IP (단일 PC) 또는 회사 공인 IP 대역
5. **Save**
6. 등록 직후 약 1분 내 적용

> 등록 안 하면 비밀번호가 맞아도 connection refused.

---

## 7. 적용 사례

### ktdb DB (2026-05-06)

| 항목 | 결과 |
|---|---|
| DB | `ktdb` 생성, owner = `ktdb` |
| Role | `ktdb` 생성 (LOGIN only) |
| Step 3 | 생략 (energy 분양 시 1회 처리됨) |
| 검증 6항목 | 모두 PASS |
| 자기 DB DDL/DML | CREATE/INSERT/SELECT/DROP 통과 |
| 격리 | ktdb → dde-water/postgres 모두 `permission denied` 확인 |
| 적용 스키마 | `db_schema_smr.sql` 30 테이블 (9 부모 + 21 파티션) |
| 자동화 | `scripts/provision_ktdb.py` (--dry-run + 본 분양) |

자동화 스크립트가 §3.2 Step 4 사고 방지 (`current_database()` 검증)를 내장.

### 첫 적용 사례 — energy DB (2026-04-30)

### 배경
타 팀 데이터 적재용 DB 분양 요청. PostGIS 미사용, 일반 RDB 워크로드.

### 결과 (요약)

| 항목 | 결과 |
|---|---|
| DB | `energy` 생성, owner = `energy` |
| Role | `energy` 생성 (LOGIN only, admin 속성 없음) |
| dde-water ACL | `{=Tc/postgres, ...}` → `{postgres=CTc/postgres, water=c/postgres, waterviewer=c/postgres}` (PUBLIC 제거) |
| postgres DB ACL | `None` → `{azure_pg_admin=CTc/azure_pg_admin}` (PUBLIC 제거) |
| 격리 검증 | energy → dde-water/postgres 모두 `permission denied` 확인 |
| 자기 DB DDL/DML | CREATE SCHEMA, TABLE, INSERT, SELECT, DROP 모두 통과 |

### 주요 명령 이력

1. port 5432 직접 연결 (admin)
2. `CREATE ROLE energy WITH LOGIN PASSWORD ...`
3. `CREATE DATABASE energy OWNER energy`
4. `REVOKE CONNECT, TEMPORARY ON DATABASE "dde-water" FROM PUBLIC`
5. `REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC`
6. (energy DB 안) `ALTER SCHEMA public OWNER TO energy; GRANT ALL ON SCHEMA public TO energy;`

비밀번호는 `.env` 외부에 노출하지 않음. 인수 팀에게는 별도 채널로 전달.

### 사고 이력

#### 2026-05-06 — DEV `dde-water` schema OWNER 영향 (의도적 미복구)

**증상**
- DEV(`geo-spatial-hub`) `dde-water`에서 `water` 계정으로 `SELECT ... FROM public.<table>` 시 `permission denied for schema public` (SQLSTATE 42501).

**근본 원인**
- 2026-04-30 energy DB 분양 시, 위 §3.2 **Step 4**(`ALTER SCHEMA public OWNER TO energy; GRANT ALL ON SCHEMA public TO energy;`)를 **`energy` DB가 아닌 `dde-water` 컨텍스트에서 실행**한 것으로 판정.
- 결과: DEV `dde-water` schema public OWNER가 `azure_pg_admin` → `energy`로 변경, `water`·`waterviewer`의 schema USAGE 상실.

**확인 결과 (2026-05-06)**
```sql
-- DEV dde-water (water 계정)
SELECT n.nspname, r.rolname AS owner FROM pg_namespace n
JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname='public';
-- public | energy

SELECT has_schema_privilege('water','public','USAGE'),
       has_schema_privilege('water','public','CREATE');
-- false | false
```

- **PROD(`geo-spatial-hub-prod`) `dde-water` 무사 확인**: owner=`azure_pg_admin`, water USAGE=true. 운영 영향 0.

**복구 SQL (참고용 — 미실행)**
```sql
-- (admin, port 5432, DB=dde-water 컨텍스트)
GRANT energy TO postgres;                              -- ALTER OWNER 권한 + admin 가시성
ALTER SCHEMA public OWNER TO azure_pg_admin;
GRANT USAGE, CREATE ON SCHEMA public TO water;
GRANT USAGE ON SCHEMA public TO waterviewer;
```

**운영 결정 (운영자 판단, 2026-05-06)**
- **미복구 유지**. DEV는 sandbox이고 admin 점검은 `postgres@5432`로 일원화하기로 결정.
- DEV `dde-water`에서 water/waterviewer 계정 사용은 당분간 불가 (PROD와 권한 비대칭).
- 인수자는 DEV에서 admin 점검 시 반드시 `postgres@5432` 사용.
- 추후 sandbox 권한 일치가 필요해질 경우 위 복구 SQL을 실행.

**재발 방지**
- §3.2 Step 4 경고 박스 추가 (실행 직전 `SELECT current_database();` 검증 1줄 필수).
- 향후 모든 분양 작업에서 Step 4·6 실행 전 컨텍스트 1줄 검증 의무화.

---

## 8. 운영 룰

| # | 룰 |
|:-:|---|
| 1 | 신규 분양은 **DEV 서버(`geo-spatial-hub`)** 에만 한다. PROD 서버 금지. |
| 2 | 신규 role에 **`CREATEDB`/`CREATEROLE`/`SUPERUSER` 절대 부여 금지**. 필요 시 관리자가 대신 처리. |
| 3 | DDL은 **port 5432**, 일반 쿼리·플러그인은 **port 6432** (CLAUDE.md 절대 룰 3). |
| 4 | 비밀번호는 `.env` 또는 별도 보안 채널에만 보관. 본 문서 등 git 추적 파일에 평문 저장 금지 (CLAUDE.md 절대 룰 4). |
| 5 | dde-water의 PUBLIC CONNECT가 회수된 상태인지 분양 전에 확인. 추가 신규 role도 자동으로 격리됨. |
| 6 | 인수 팀에 전달 전 §4 검증 6항목 **모두 통과** 확인. |
| 7 | 신규 DB 추가 시 본 문서 §7에 사례 추가 (요약 표 + 명령 이력). |

---

## 9. 향후 다른 테넌트 추가 시

본 문서 §3 절차를 그대로 반복하되, **§3.2 Step 3은 1회만**:
- 첫 분양(`energy`) 시점에 이미 dde-water·postgres에서 PUBLIC CONNECT가 회수됨
- 이후 추가되는 모든 신규 role은 PUBLIC 미상속이라 자동 격리됨
- 단, 신규 DB 자체에 대한 ACL 설정은 매번 필요

§7에 새 케이스를 1줄 표로 추가하고 마무리.

---

## 짝 문서

- [`DB_분산관리체계_분석보고서.md`](DB_분산관리체계_분석보고서.md) — 분산 관리 모델 이론적 근거
- [`../../CLAUDE.md`](../../CLAUDE.md) — 절대 룰 7개
- [`../../scripts/common/db_connection.py`](../../scripts/common/db_connection.py) — 표준 DB 접속 모듈
- [`../07_인수인계/DB_인수인계_v1.md`](../07_인수인계/DB_인수인계_v1.md) — DB 운영 인수인계
