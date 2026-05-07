"""
DB 자격증명·연결 모듈 (Azure Postgres ktdb 테넌트).

GIS 플러그인 패턴 (`scripts/common/db_connection.py`)을 미러.
Streamlit 앱·sync 스크립트 양쪽에서 공용.

자격증명 우선순위:
  1) Streamlit secrets.toml [azure] 블록 (앱 런타임)
  2) ENV 변수 KTDB_DB_* (CLI 스크립트 / CI)
  3) 명시적 인자 override (단위 테스트)

포트 정책 (CLAUDE.md 절대 룰 3):
  - 5432: DDL/대량 적재 (sync_smr.py가 사용)
  - 6432: 일반 쿼리 (Streamlit 런타임이 사용)

분양 절차: docs/테넌트_DB_분양_절차.md
PRD: docs/PRD_DB-플랫폼-Azure-전환.md
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parent
SECRETS_PATH = REPO_ROOT / ".streamlit" / "secrets.toml"


@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: str = "require"


def _make_config(az: dict) -> DBConfig:
    return DBConfig(
        host=str(az["host"]),
        port=int(az.get("port", 6432)),
        database=str(az["database"]),
        user=str(az["user"]),
        password=str(az["password"]),
        sslmode=str(az.get("sslmode", "require")),
    )


def _from_streamlit_runtime() -> DBConfig | None:
    """Streamlit 런타임에서만 동작 (앱 안에서 호출 시)."""
    try:
        import streamlit as st
    except ImportError:
        return None
    try:
        az = st.secrets["azure"]
    except Exception:
        return None
    return _make_config(dict(az))


def _from_secrets_file() -> DBConfig | None:
    """CLI 컨텍스트: .streamlit/secrets.toml 직접 파싱."""
    if not SECRETS_PATH.is_file():
        return None
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # py < 3.11 fallback
    with open(SECRETS_PATH, "rb") as f:
        data = tomllib.load(f)
    az = data.get("azure")
    if not az:
        return None
    return _make_config(az)


def _from_streamlit() -> DBConfig | None:
    """런타임 우선, 실패 시 secrets 파일 직접 파싱 (CLI 호환)."""
    cfg = _from_streamlit_runtime()
    if cfg is not None:
        return cfg
    return _from_secrets_file()


def _from_env() -> DBConfig | None:
    keys = ["KTDB_DB_HOST", "KTDB_DB_USER", "KTDB_DB_PASSWORD", "KTDB_DB_NAME"]
    if not all(os.environ.get(k) for k in keys):
        return None
    return DBConfig(
        host=os.environ["KTDB_DB_HOST"],
        port=int(os.environ.get("KTDB_DB_PORT", "6432")),
        database=os.environ["KTDB_DB_NAME"],
        user=os.environ["KTDB_DB_USER"],
        password=os.environ["KTDB_DB_PASSWORD"],
        sslmode=os.environ.get("KTDB_DB_SSLMODE", "require"),
    )


def get_config(*, ddl: bool = False) -> DBConfig:
    """ddl=True면 5432 포트로 강제 (DDL/대량 적재용)."""
    cfg = _from_streamlit() or _from_env()
    if cfg is None:
        raise RuntimeError(
            "DB config not found. Provide one of:\n"
            "  - .streamlit/secrets.toml [azure] block, or\n"
            "  - ENV: KTDB_DB_HOST, KTDB_DB_USER, KTDB_DB_PASSWORD, KTDB_DB_NAME"
        )
    if ddl and cfg.port != 5432:
        cfg = DBConfig(
            host=cfg.host, port=5432, database=cfg.database,
            user=cfg.user, password=cfg.password, sslmode=cfg.sslmode,
        )
    return cfg


def connect(*, ddl: bool = False, autocommit: bool = False) -> psycopg2.extensions.connection:
    """psycopg2 connection 생성. ddl=True면 5432 + autocommit=True 권장."""
    cfg = get_config(ddl=ddl)
    conn = psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.database,
        user=cfg.user,
        password=cfg.password,
        sslmode=cfg.sslmode,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )
    conn.autocommit = autocommit or ddl
    return conn


def health_check() -> dict:
    """간단한 연결 테스트. CLI에서 `python -c 'from db_env import health_check; print(health_check())'`."""
    cfg = get_config()
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version()")
            db, user, ver = cur.fetchone()
        return {
            "ok": True,
            "host": cfg.host,
            "port": cfg.port,
            "database": db,
            "user": user,
            "version": ver.split(",")[0],
        }
    except Exception as e:
        return {"ok": False, "host": cfg.host, "port": cfg.port, "error": str(e)[:200]}


if __name__ == "__main__":
    import json
    print(json.dumps(health_check(), ensure_ascii=False, indent=2))
