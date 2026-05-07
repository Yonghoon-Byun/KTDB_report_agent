"""
KTDB Read-Only 계정(`ktdb_viewer`) 분양 스크립트.

GIS 플러그인 모델(`waterviewer` R/O 패턴)을 KTDB에도 동일하게 적용.
사용자에게 분배할 secrets.toml에 평문 저장해도 위험도 낮음 (SELECT only).

요구 사항:
  - DEV 서버: geo-spatial-hub.postgres.database.azure.com
  - port 5432 (DDL용 직접 연결)
  - 관리자 계정: postgres (CREATE ROLE 권한)
  - ktdb 소유자 계정: ktdb (테이블 GRANT 권한)
  - 작업자 본인 공인 IP가 Azure 방화벽에 등록되어 있어야 함

비밀번호 우선순위:
  - admin (postgres):    ENV ADMIN_PG_PASSWORD → .env OLD_DB_PASSWORD → stdin
  - tenant owner (ktdb): ENV KTDB_DB_PASSWORD  → .env KTDB_DB_PASSWORD → stdin
  - viewer (ktdb_viewer): ENV KTDB_VIEWER_PASSWORD → .env KTDB_VIEWER_PASSWORD → stdin

권한 정책 (CLAUDE.md 절대 룰 4 준수):
  - CREATEDB / CREATEROLE / SUPERUSER 절대 부여 금지
  - LOGIN + CONNECT(ktdb) + USAGE(public) + SELECT(all tables) 만 부여
  - 향후 추가될 테이블에도 자동으로 SELECT 부여 (ALTER DEFAULT PRIVILEGES)

검증:
  - 양성: ktdb_viewer로 SELECT count(*) FROM smr_zones 성공
  - 음성: INSERT/UPDATE/DELETE/CREATE/DROP 모두 permission denied

실행:
  python scripts/provision_ktdb_viewer.py             # 분양
  python scripts/provision_ktdb_viewer.py --dry-run   # 환경/접속만 확인
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

OWNER_ROLE = "ktdb"
TENANT_DB = "ktdb"

VIEWER_ROLE = "ktdb_viewer"
VIEWER_PASSWORD_ENV = "KTDB_VIEWER_PASSWORD"
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
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,))
    return cur.fetchone() is not None


def step1_create_role(admin_conn, viewer_password: str) -> None:
    """admin 권한으로 ktdb_viewer LOGIN role만 생성. 권한 부여는 Step 2에서."""
    with admin_conn.cursor() as cur:
        if _exists_role(cur, VIEWER_ROLE):
            print(f"  [skip] role '{VIEWER_ROLE}' already exists — password 갱신")
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(
                    sql.Identifier(VIEWER_ROLE)
                ),
                (viewer_password,),
            )
            print(f"  [+] ALTER ROLE {VIEWER_ROLE} PASSWORD updated")
            return
        cur.execute(
            sql.SQL(
                "CREATE ROLE {} WITH LOGIN PASSWORD %s "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
            ).format(sql.Identifier(VIEWER_ROLE)),
            (viewer_password,),
        )
        print(f"  [+] CREATE ROLE {VIEWER_ROLE} (R/O, LOGIN only)")


def step2_grant_select(owner_password: str) -> None:
    """ktdb 소유자 권한으로 SELECT 부여. 향후 테이블에도 자동 적용."""
    owner_conn = _connect(TENANT_DB, OWNER_ROLE, owner_password)
    try:
        with owner_conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            current = cur.fetchone()[0]
            if current != TENANT_DB:
                raise RuntimeError(
                    f"context check failed: current_database()={current!r}, expected {TENANT_DB!r}"
                )
            print(f"  [check] current_database() = {current!r} OK")

            # CONNECT 권한
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(TENANT_DB), sql.Identifier(VIEWER_ROLE)
                )
            )
            # USAGE on schema
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(VIEWER_ROLE)
                )
            )
            # 기존 테이블 SELECT
            cur.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                    sql.Identifier(VIEWER_ROLE)
                )
            )
            # 기존 시퀀스 USAGE/SELECT (currval/lastval 조회용, 데이터 변경 불가)
            cur.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(VIEWER_ROLE)
                )
            )
            # 향후 추가될 테이블에도 자동 SELECT
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO {}"
                ).format(sql.Identifier(VIEWER_ROLE))
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}"
                ).format(sql.Identifier(VIEWER_ROLE))
            )
            print(
                f"  [+] GRANT CONNECT/USAGE/SELECT to {VIEWER_ROLE} (현재 + 향후 테이블)"
            )
    finally:
        owner_conn.close()


def verify(viewer_password: str) -> bool:
    """양성/음성 테스트로 R/O 격리 확인."""
    print("\n=== 검증 ===")
    all_ok = True

    try:
        viewer_conn = _connect(TENANT_DB, VIEWER_ROLE, viewer_password)
    except psycopg2.OperationalError as e:
        msg = str(e).strip().splitlines()[0]
        print(f"  [FAIL] viewer 로그인 실패: {msg[:200]}")
        return False

    try:
        with viewer_conn.cursor() as cur:
            # ① role 속성: LOGIN만 True, 나머지 False
            cur.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, "
                "       rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname=%s",
                (VIEWER_ROLE,),
            )
            super_, cdb, crole, login, repl, bypass = cur.fetchone()
            ok1 = (
                not super_ and not cdb and not crole and login and not repl and not bypass
            )
            all_ok &= ok1
            print(
                f"  {'[OK]' if ok1 else '[FAIL]'} ① role 속성: "
                f"super={super_} createdb={cdb} createrole={crole} "
                f"login={login} replication={repl} bypassrls={bypass}"
            )

            # ② SELECT 정상 동작
            cur.execute("SELECT count(*) FROM smr_zones")
            zones_cnt = cur.fetchone()[0]
            ok2 = zones_cnt > 0
            all_ok &= ok2
            print(
                f"  {'[OK]' if ok2 else '[FAIL]'} ② SELECT smr_zones → {zones_cnt}행"
            )

            cur.execute("SELECT count(*) FROM smr_od_purpose")
            od_cnt = cur.fetchone()[0]
            ok3 = od_cnt > 0
            all_ok &= ok3
            print(
                f"  {'[OK]' if ok3 else '[FAIL]'} ③ SELECT smr_od_purpose → {od_cnt:,}행"
            )

        # ③ 음성 테스트: INSERT/CREATE 거부 확인
        for stmt, label in [
            ("INSERT INTO smr_zones (taz_seq) VALUES (99999)", "INSERT"),
            ("UPDATE smr_zones SET taz_seq=1 WHERE taz_seq=1", "UPDATE"),
            ("DELETE FROM smr_zones WHERE taz_seq=99999", "DELETE"),
            ("CREATE TABLE _viewer_check (n INT)", "CREATE TABLE"),
            ("DROP TABLE smr_zones", "DROP TABLE"),
        ]:
            with viewer_conn.cursor() as cur:
                try:
                    cur.execute(stmt)
                    print(f"  [FAIL] ④ {label} should be denied but succeeded")
                    all_ok = False
                except psycopg2.errors.InsufficientPrivilege:
                    print(f"  [OK]   ④ {label} → permission denied (정상)")
                except psycopg2.Error as e:
                    msg = str(e).strip().splitlines()[0]
                    print(f"  [OK]   ④ {label} → {msg[:80]}")
    finally:
        viewer_conn.close()

    print()
    print(f"전체 검증: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


def _load_dotenv(path: Path) -> dict[str, str]:
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


def _resolve_password(
    *, env_var: str, dotenv_keys: list[str], prompt: str, allow_prompt: bool = True
) -> str | None:
    if os.environ.get(env_var):
        print(f"  [src] password from {env_var} env")
        return os.environ[env_var]

    repo_root = Path(__file__).resolve().parent.parent
    dotenv = _load_dotenv(repo_root / ".env")
    for key in dotenv_keys:
        if dotenv.get(key):
            print(f"  [src] password from .env {key}")
            return dotenv[key]

    if not allow_prompt:
        print(f"  [warn] {env_var} not set (skipped prompt)")
        return None
    return getpass.getpass(prompt) or None


def main() -> int:
    parser = argparse.ArgumentParser(description="KTDB Read-Only 계정 분양")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="환경/접속만 확인 (DDL 미실행)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print(f" KTDB Viewer (R/O) {'분양 (DRY-RUN)' if args.dry_run else '분양'}")
    print(f"   host         = {DEV_HOST}")
    print(f"   port (DDL)   = {DDL_PORT}")
    print(f"   target role  = {VIEWER_ROLE}")
    print(f"   target DB    = {TENANT_DB}")
    print(f"   권한         = LOGIN + CONNECT + USAGE + SELECT")
    print("=" * 60)

    print("\n[자격증명 로드]")
    admin_password = _resolve_password(
        env_var="ADMIN_PG_PASSWORD",
        dotenv_keys=["OLD_DB_PASSWORD"],
        prompt=f"[admin] {ADMIN_USER}@{DEV_HOST}: ",
        allow_prompt=not args.dry_run,
    )
    if not args.dry_run and not admin_password:
        print("ERROR: admin password is empty")
        return 2

    owner_password = _resolve_password(
        env_var=TENANT_PASSWORD_ENV,
        dotenv_keys=[TENANT_PASSWORD_ENV],
        prompt=f"[owner] {OWNER_ROLE}@{TENANT_DB}: ",
        allow_prompt=False,
    )
    if not owner_password:
        # Fallback: db_env가 .streamlit/secrets.toml에서 owner 자격증명 추출
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from db_env import _from_secrets_file  # type: ignore

            cfg = _from_secrets_file()
            if cfg and cfg.user == OWNER_ROLE:
                owner_password = cfg.password
                print(f"  [src] owner password from .streamlit/secrets.toml [azure]")
        except Exception as e:
            print(f"  [warn] secrets.toml fallback failed: {e}")

    if not args.dry_run and not owner_password:
        print(f"ERROR: owner password is empty ({TENANT_PASSWORD_ENV})")
        return 2

    viewer_password = _resolve_password(
        env_var=VIEWER_PASSWORD_ENV,
        dotenv_keys=[VIEWER_PASSWORD_ENV],
        prompt=f"[viewer] {VIEWER_ROLE} 신규 비밀번호 (분배용, R/O): ",
        allow_prompt=not args.dry_run,
    )
    if not args.dry_run and not viewer_password:
        print(f"ERROR: viewer password is empty ({VIEWER_PASSWORD_ENV})")
        return 2

    print("\n[admin 접속 테스트]")
    try:
        admin_conn = _connect(ADMIN_DB, ADMIN_USER, admin_password)
    except psycopg2.OperationalError as e:
        msg = str(e).strip().splitlines()[0]
        print(f"FAIL: admin 접속 불가 — {msg[:200]}")
        return 3

    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version()")
            cdb, cuser, ver = cur.fetchone()
            print(f"  [OK] connected: db={cdb} user={cuser}")
            print(f"  [info] {ver.split(',')[0]}")
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (VIEWER_ROLE,))
            viewer_exists = cur.fetchone() is not None
            print(f"  [info] role '{VIEWER_ROLE}' exists? {viewer_exists}")

        if args.dry_run:
            print("\n" + "=" * 60)
            print(" DRY-RUN 완료. DDL 미실행.")
            print(" 분양 진행:")
            print("   $env:KTDB_VIEWER_PASSWORD = '<원하는 비밀번호>'")
            print("   python scripts/provision_ktdb_viewer.py")
            print("=" * 60)
            return 0

        print("\n[Step 1] CREATE/ALTER ROLE ktdb_viewer (admin 권한)")
        step1_create_role(admin_conn, viewer_password)

        print("\n[Step 2] GRANT CONNECT/USAGE/SELECT (ktdb owner 권한)")
        step2_grant_select(owner_password)

        print("\n[검증]")
        if not verify(viewer_password):
            print("\nFAIL: 검증 실패. 위 로그 확인.")
            return 1

        print("\n" + "=" * 60)
        print(" Viewer 분양 완료. 분배 절차:")
        print("   1) .streamlit/secrets.toml.example의 [azure] 블록을 viewer 자격증명으로 교체")
        print("   2) 사용자에게 코드 + secrets.toml + docs/사용자-설치-가이드.md 전달")
        print("   3) 사용자는 Azure 방화벽 IP 등록 후 streamlit run streamlit_app.py")
        print("=" * 60)
        return 0
    finally:
        admin_conn.close()


if __name__ == "__main__":
    sys.exit(main())
