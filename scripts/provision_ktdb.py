"""
KTDB DB 분양 자동화 스크립트.

테넌트_DB_분양_절차.md §3 절차를 그대로 따라 ktdb DB·role을 신설하고
§4 검증 6항목을 자동 확인한다.

요구 사항:
  - DEV 서버: geo-spatial-hub.postgres.database.azure.com
  - port 5432 (DDL용 직접 연결, PgBouncer 6432 사용 금지)
  - 관리자 계정: postgres
  - 작업자 본인 공인 IP가 Azure 방화벽에 등록되어 있어야 함

비밀번호 우선순위:
  1) ENV ADMIN_PG_PASSWORD
  2) .env 파일의 OLD_DB_PASSWORD (OLD_DB_USER가 postgres일 때만)
  3) stdin getpass

사고 방지:
  - Step 4(public schema OWNER 변경)는 ktdb 컨텍스트에서만 실행
  - 실행 직전 SELECT current_database() 검증 1줄 의무화 (§7 사고 이력 재발 방지)
  - .env에 port=6432가 있어도 무시. DDL은 항상 5432로 강제 (절대 룰 3)

실행:
  python scripts/provision_ktdb.py             # 실제 분양
  python scripts/provision_ktdb.py --dry-run   # admin 접속 테스트만

idempotent:
  - role/DB가 이미 존재하면 SKIP하고 검증만 재실행
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql

DEV_HOST = "geo-spatial-hub.postgres.database.azure.com"
DDL_PORT = 5432
ADMIN_USER = "postgres"
ADMIN_DB = "postgres"

NEW_ROLE = "ktdb"
NEW_DB = "ktdb"
TENANT_PASSWORD_ENV = "KTDB_DB_PASSWORD"


def _connect(dbname: str, user: str, password: str) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(
        host=DEV_HOST,
        port=DDL_PORT,
        user=user,
        password=password,
        dbname=dbname,
        sslmode="require",
        connect_timeout=10,
    )
    conn.autocommit = True
    return conn


def _exists_role(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


def _exists_db(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    return cur.fetchone() is not None


def step1_create_role(admin_conn, tenant_password: str) -> None:
    with admin_conn.cursor() as cur:
        if _exists_role(cur, NEW_ROLE):
            print(f"  [skip] role '{NEW_ROLE}' already exists")
            return
        cur.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(NEW_ROLE)),
            (tenant_password,),
        )
        print(f"  [+] CREATE ROLE {NEW_ROLE}")


def step2_create_database(admin_conn) -> None:
    with admin_conn.cursor() as cur:
        if _exists_db(cur, NEW_DB):
            print(f"  [skip] database '{NEW_DB}' already exists")
            return
        cur.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(NEW_DB), sql.Identifier(NEW_ROLE)
            )
        )
        print(f"  [+] CREATE DATABASE {NEW_DB} OWNER {NEW_ROLE}")


def step4_fix_schema(admin_password: str) -> None:
    """ktdb 컨텍스트에서 public schema OWNER + GRANT 정리.

    §7 사고(2026-05-06) 재발 방지: 반드시 ktdb DB로 접속한 뒤 current_database()
    검증 후에만 ALTER 실행.
    """
    new_db_conn = _connect(NEW_DB, ADMIN_USER, admin_password)
    try:
        with new_db_conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            current = cur.fetchone()[0]
            if current != NEW_DB:
                raise RuntimeError(
                    f"context check failed: current_database()={current!r}, expected {NEW_DB!r}. "
                    "Step 4 aborted to prevent §7 incident recurrence."
                )
            print(f"  [check] current_database() = {current!r} OK")

            cur.execute(
                sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(NEW_ROLE))
            )
            cur.execute(
                sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(NEW_ROLE))
            )
            print(f"  [+] ALTER SCHEMA public OWNER TO {NEW_ROLE}; GRANT ALL")
    finally:
        new_db_conn.close()


def verify(admin_conn, tenant_password: str) -> bool:
    """§4 검증 6항목 자동 체크."""
    print("\n=== 검증 (§4) ===")
    all_ok = True

    with admin_conn.cursor() as cur:
        # ① role 속성: 모두 F여야 함 (canlogin만 T)
        cur.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, "
            "       rolcanlogin, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname=%s",
            (NEW_ROLE,),
        )
        row = cur.fetchone()
        if row is None:
            print(f"  [FAIL] ① role '{NEW_ROLE}' not found")
            return False
        _, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolreplication, rolbypassrls = row
        ok1 = (
            not rolsuper
            and not rolcreatedb
            and not rolcreaterole
            and rolcanlogin
            and not rolreplication
            and not rolbypassrls
        )
        all_ok &= ok1
        print(
            f"  {'[OK]' if ok1 else '[FAIL]'} ① role 속성: "
            f"super={rolsuper} createdb={rolcreatedb} createrole={rolcreaterole} "
            f"canlogin={rolcanlogin} replication={rolreplication} bypassrls={rolbypassrls}"
        )

        # ② ACL 확인
        cur.execute(
            "SELECT datname, datacl::text FROM pg_database "
            "WHERE datname IN ('dde-water','postgres',%s) ORDER BY datname",
            (NEW_DB,),
        )
        for datname, datacl in cur.fetchall():
            print(f"  [info] ② {datname}: {datacl}")

        # ③④⑤ 권한 매트릭스
        cur.execute(
            "SELECT has_database_privilege(%s,'dde-water','CONNECT'), "
            "       has_database_privilege(%s,'postgres','CONNECT'), "
            "       has_database_privilege(%s,%s,'CONNECT')",
            (NEW_ROLE, NEW_ROLE, NEW_ROLE, NEW_DB),
        )
        can_dde, can_pg, can_self = cur.fetchone()
        ok345 = (not can_dde) and (not can_pg) and can_self
        all_ok &= ok345
        print(
            f"  {'[OK]' if ok345 else '[FAIL]'} ③④⑤ 권한 매트릭스: "
            f"can_dde={can_dde} can_pg={can_pg} can_self={can_self} "
            f"(기대: false/false/true)"
        )

    # ⑥ 신규 role 직접 로그인 + DDL/DML 실행
    try:
        user_conn = _connect(NEW_DB, NEW_ROLE, tenant_password)
        try:
            with user_conn.cursor() as cur:
                cur.execute("CREATE TABLE _provision_check (n INT)")
                cur.execute("INSERT INTO _provision_check VALUES (1)")
                cur.execute("SELECT count(*) FROM _provision_check")
                cnt = cur.fetchone()[0]
                cur.execute("DROP TABLE _provision_check")
                ok6 = cnt == 1
                all_ok &= ok6
                print(
                    f"  {'[OK]' if ok6 else '[FAIL]'} ⑥ {NEW_ROLE}@{NEW_DB} "
                    f"CREATE/INSERT/SELECT/DROP all passed"
                )
        finally:
            user_conn.close()
    except Exception as e:
        all_ok = False
        print(f"  [FAIL] ⑥ {NEW_ROLE}@{NEW_DB} login or DDL failed: {e}")

    # ④⑤ 격리 확인 (신규 role이 dde-water·postgres 접속 시도 → permission denied)
    for db in ("dde-water", "postgres"):
        try:
            test_conn = _connect(db, NEW_ROLE, tenant_password)
            test_conn.close()
            print(f"  [FAIL] ④⑤ {NEW_ROLE} should NOT be able to connect to {db}")
            all_ok = False
        except psycopg2.OperationalError as e:
            msg = str(e).strip().splitlines()[0] if str(e) else ""
            print(f"  [OK]   ④⑤ {NEW_ROLE} → {db} blocked ({msg[:80]})")

    print()
    print(f"전체 검증: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env loader (no external dep). Skips comments and blank lines."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    pat = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = pat.match(ln)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        env[k] = v
    return env


def _resolve_admin_password() -> str | None:
    """우선순위: ADMIN_PG_PASSWORD env → .env (OLD_DB_USER==postgres) → stdin."""
    if os.environ.get("ADMIN_PG_PASSWORD"):
        print("  [src] password from ADMIN_PG_PASSWORD env")
        return os.environ["ADMIN_PG_PASSWORD"]

    repo_root = Path(__file__).resolve().parent.parent
    dotenv = _load_dotenv(repo_root / ".env")
    if dotenv.get("OLD_DB_USER") == ADMIN_USER and dotenv.get("OLD_DB_PASSWORD"):
        host = dotenv.get("OLD_DB_HOST", "")
        if "geo-spatial-hub" in host and "-prod" not in host:
            print("  [src] password from .env OLD_DB_PASSWORD (DEV admin)")
            return dotenv["OLD_DB_PASSWORD"]
        else:
            print(f"  [warn] .env host '{host}' is not DEV — refusing auto-load")
    else:
        print(
            "  [warn] .env OLD_DB_USER != 'postgres' — auto-load skipped "
            f"(found user={dotenv.get('OLD_DB_USER')!r})"
        )

    return getpass.getpass(f"[admin] {ADMIN_USER}@{DEV_HOST}:{DDL_PORT} 비밀번호: ") or None


def _resolve_tenant_password(*, required: bool) -> str | None:
    """우선순위: ENV KTDB_DB_PASSWORD → .env KTDB_DB_PASSWORD → stdin."""
    if os.environ.get(TENANT_PASSWORD_ENV):
        print(f"  [src] tenant password from {TENANT_PASSWORD_ENV} env")
        return os.environ[TENANT_PASSWORD_ENV]

    repo_root = Path(__file__).resolve().parent.parent
    dotenv = _load_dotenv(repo_root / ".env")
    if dotenv.get(TENANT_PASSWORD_ENV):
        print(f"  [src] tenant password from .env {TENANT_PASSWORD_ENV}")
        return dotenv[TENANT_PASSWORD_ENV]

    if not required:
        print(f"  [warn] {TENANT_PASSWORD_ENV} not provided")
        return None

    prompt = f"[tenant] {NEW_ROLE}@{DEV_HOST}:{DDL_PORT} 비밀번호 ({TENANT_PASSWORD_ENV}): "
    return getpass.getpass(prompt) or None


def main() -> int:
    parser = argparse.ArgumentParser(description="KTDB 분양 스크립트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="admin 접속 + 환경 검증만 수행 (DDL 미실행, 사용자 GO 전 점검용)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print(f" KTDB {'분양 (DRY-RUN)' if args.dry_run else '분양'} 스크립트")
    print(f"   host        = {DEV_HOST}")
    print(f"   port (DDL)  = {DDL_PORT}   ← 절대 룰 3: PgBouncer 6432 사용 금지")
    print(f"   admin user  = {ADMIN_USER}")
    print(f"   target role = {NEW_ROLE}")
    print(f"   target DB   = {NEW_DB}")
    print("=" * 60)

    print("\n[자격증명 로드]")
    admin_password = _resolve_admin_password()
    if not admin_password:
        print("ERROR: admin password is empty")
        return 2
    tenant_password = _resolve_tenant_password(required=not args.dry_run)
    if not args.dry_run and not tenant_password:
        print(f"ERROR: tenant password is empty ({TENANT_PASSWORD_ENV})")
        return 2

    print("\n[admin 접속 테스트] postgres DB / autocommit=True")
    try:
        admin_conn = _connect(ADMIN_DB, ADMIN_USER, admin_password)
    except psycopg2.OperationalError as e:
        msg = str(e).strip().splitlines()[0]
        print(f"FAIL: admin 접속 불가 — {msg[:200]}")
        print("\n원인 후보:")
        print("  - Azure 방화벽에 작업자 IP 미등록 (whatismyip.com 으로 확인 후 IT 등록 요청)")
        print("  - 비밀번호 불일치 (.env 또는 ENV ADMIN_PG_PASSWORD 값)")
        print("  - 5432 포트 차단 (회사 네트워크에서 outbound 5432 허용 필요)")
        return 3

    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version()")
            cdb, cuser, ver = cur.fetchone()
            print(f"  [OK] connected: db={cdb} user={cuser}")
            print(f"  [info] {ver.split(',')[0]}")

            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (NEW_ROLE,))
            role_exists = cur.fetchone() is not None
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (NEW_DB,))
            db_exists = cur.fetchone() is not None
            print(f"  [info] role '{NEW_ROLE}' exists? {role_exists}")
            print(f"  [info] database '{NEW_DB}' exists? {db_exists}")

        if args.dry_run:
            print()
            print("=" * 60)
            print(" DRY-RUN 완료. DDL은 실행되지 않았습니다.")
            print(" 분양을 진행하려면 사용자 명시 GO 후:")
            print("   python scripts/provision_ktdb.py")
            print(f" 비밀번호는 ENV 또는 .env 의 {TENANT_PASSWORD_ENV} 로 제공하세요.")
            print("=" * 60)
            return 0

        print("\n[Step 1] CREATE ROLE")
        step1_create_role(admin_conn, tenant_password)

        print("\n[Step 2] CREATE DATABASE")
        step2_create_database(admin_conn)

        print("\n[Step 3] PUBLIC CONNECT 회수 (energy 분양 시 1회 처리됨, 생략)")
        print("  [skip] §9 운영 룰: dde-water/postgres PUBLIC 회수는 첫 분양 1회만")

        print("\n[Step 4] public schema OWNER + GRANT (ktdb 컨텍스트)")
        step4_fix_schema(admin_password)

        print("\n[검증] §4 6항목")
        if not verify(admin_conn, tenant_password):
            print("\nFAIL: 일부 검증 항목 실패. 절차 문서 §4 참조 후 재시도.")
            return 1

        print()
        print("=" * 60)
        print(" 분양 완료. 다음 단계:")
        print("   1) .streamlit/secrets.toml에 [azure] 블록 추가 (예: secrets.toml.example 참조)")
        print("   2) docs/테넌트_DB_분양_절차.md §7에 ktdb 케이스 1줄 추가")
        print("   3) sync_smr.py 작성 → 7년 데이터 적재")
        print("=" * 60)
        return 0
    finally:
        admin_conn.close()


if __name__ == "__main__":
    sys.exit(main())
