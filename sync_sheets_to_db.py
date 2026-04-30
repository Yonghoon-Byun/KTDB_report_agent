"""
KTDB 통합 분석 에이전트 — Google Sheets → Supabase Postgres 동기화 스크립트.

OAuth 없이 시트 link-share의 CSV export URL로 직접 다운로드.
(서비스 계정 인증이 회사 보안 소프트웨어에 차단되는 환경 대응)

사용법:
    1) CLI:           python sync_sheets_to_db.py
    2) Streamlit:     from sync_sheets_to_db import sync_all
                      report = sync_all(conn, sheet_urls)

idempotent — 매 실행 시 TRUNCATE 후 재적재.
"""
from __future__ import annotations

import io
import re
import time
import urllib.request

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values


YEARS = [2023, 2025, 2030, 2035, 2040, 2045, 2050]

SOCIO_INDICATORS = ["POP_TOT", "POP_YNG", "POP_15P", "EMP", "STU", "WORK_TOT"]


# ─────────────────────────────────────────────────────────────
# 시트 로드 (CSV export URL, OAuth 없음)
# ─────────────────────────────────────────────────────────────
def _extract_sheet_id(url: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"시트 ID 추출 실패: {url}")
    return m.group(1)


def _fetch_tab(sheet_url: str, tab: str, timeout: int = 60) -> pd.DataFrame:
    sid = _extract_sheet_id(sheet_url)
    csv_url = (f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq"
               f"?tqx=out:csv&sheet={tab}")
    req = urllib.request.urlopen(csv_url, timeout=timeout)
    body = req.read()
    if not body or body.startswith(b"<"):
        raise ValueError(f"비어있거나 HTML 응답: {tab}")
    return pd.read_csv(io.BytesIO(body), dtype=str, keep_default_na=False)


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False),
                         errors="coerce")


# ─────────────────────────────────────────────────────────────
# 테이블별 적재
# ─────────────────────────────────────────────────────────────
def load_zones(cur, socio_url: str) -> int:
    df = _fetch_tab(socio_url, "ZONE")
    df = df[["SIDO", "SIGU", "ZONE"]].copy()
    df["ZONE"] = _to_numeric(df["ZONE"])
    df = df.dropna(subset=["ZONE"])
    df["ZONE"] = df["ZONE"].astype(int)
    df["SIDO"] = df["SIDO"].fillna("").astype(str).str.strip()
    df["SIGU"] = df["SIGU"].fillna("").astype(str).str.strip()
    df = df[(df["SIDO"] != "") & (df["SIGU"] != "")].drop_duplicates(subset=["ZONE"])

    cur.execute("TRUNCATE TABLE zones CASCADE")
    rows = list(df[["ZONE", "SIDO", "SIGU"]].itertuples(index=False, name=None))
    execute_values(cur, "INSERT INTO zones (zone, sido, sigu) VALUES %s",
                   rows, page_size=1000)
    return len(rows)


def load_socio(cur, socio_url: str) -> int:
    cur.execute("TRUNCATE TABLE socio")
    cur.execute("SELECT zone FROM zones")
    valid_zones = {r[0] for r in cur.fetchall()}

    total = 0
    for indicator in SOCIO_INDICATORS:
        try:
            df = _fetch_tab(socio_url, indicator)
        except Exception as e:
            print(f"  [!] {indicator}: {e} - 스킵", flush=True)
            continue

        # C1 헤더가 빈 칸이면 pandas가 'Unnamed: 2'로 변환 → 3번째 컬럼을 ZONE으로 가정
        if "ZONE" not in df.columns and len(df.columns) >= 3:
            third = df.columns[2]
            if str(third).startswith("Unnamed") or str(third).strip() == "":
                df = df.rename(columns={third: "ZONE"})

        if "ZONE" not in df.columns:
            print(f"  [!] {indicator}: ZONE 컬럼 없음 (cols={list(df.columns)[:5]}) - 스킵", flush=True)
            continue
        df["ZONE"] = _to_numeric(df["ZONE"])
        df = df.dropna(subset=["ZONE"])
        df["ZONE"] = df["ZONE"].astype(int)

        year_cols = [c for c in df.columns if c.isdigit() and int(c) in YEARS]
        if not year_cols:
            print(f"  [!] {indicator}: 연도 컬럼 없음 - 스킵", flush=True)
            continue

        long = df.melt(id_vars=["ZONE"], value_vars=year_cols,
                       var_name="year", value_name="value")
        long["year"] = long["year"].astype(int)
        long["value"] = _to_numeric(long["value"])
        long = long[long["ZONE"].isin(valid_zones)]
        long = long.dropna(subset=["value"])
        long.insert(0, "indicator_code", indicator)

        rows = list(long[["indicator_code", "ZONE", "year", "value"]]
                    .itertuples(index=False, name=None))
        if not rows:
            continue
        execute_values(cur,
            "INSERT INTO socio (indicator_code, zone, year, value) VALUES %s",
            rows, page_size=2000)
        total += len(rows)
        print(f"  [+] {indicator}: {len(rows):,}행", flush=True)
    return total


def _load_od_table(cur, sheet_url: str, table_name: str,
                   tab_prefix: str, value_cols: list[str]) -> int:
    cur.execute(f"TRUNCATE TABLE {table_name}")
    cols_sql = ", ".join(["year", "orgn", "dest"] + [c.lower() for c in value_cols])
    insert_sql = f"INSERT INTO {table_name} ({cols_sql}) VALUES %s"

    total = 0
    for y in YEARS:
        tab = f"{tab_prefix}_{y}"
        try:
            df = _fetch_tab(sheet_url, tab)
        except Exception as e:
            print(f"  [!] {tab}: {e} - 스킵", flush=True)
            continue

        required = ["ORGN", "DEST"] + value_cols
        missing = [c for c in required if c not in df.columns]
        if missing:
            print(f"  [!] {tab}: 누락 컬럼 {missing} - 스킵", flush=True)
            continue

        df = df[required].copy()
        for col in required:
            df[col] = _to_numeric(df[col])
        df = df.dropna(subset=["ORGN", "DEST"])
        df["ORGN"] = df["ORGN"].astype(int)
        df["DEST"] = df["DEST"].astype(int)
        df.insert(0, "year", y)

        rows = list(df.itertuples(index=False, name=None))
        if not rows:
            continue
        execute_values(cur, insert_sql, rows, page_size=2000)
        total += len(rows)
        print(f"  [+] {tab}: {len(rows):,}행", flush=True)
    return total


def load_od_purpose(cur, url: str) -> int:
    return _load_od_table(cur, url, "od_purpose", "PUR",
                          ["WORK", "SCHO", "BUSI", "HOME", "OTHE"])


def load_od_main_mode(cur, url: str) -> int:
    return _load_od_table(cur, url, "od_main_mode", "MOD",
                          ["AUTO", "OBUS", "SUBW", "RAIL", "ERAI"])


def load_od_access_mode(cur, url: str) -> int:
    cur.execute("TRUNCATE TABLE od_access_mode")
    df = _fetch_tab(url, "ATTMOD_2023")
    required = ["ORGN", "DEST", "ATT_AANT", "ATT_OBUS"]
    df = df[required].copy()
    for c in required:
        df[c] = _to_numeric(df[c])
    df = df.dropna(subset=["ORGN", "DEST"])
    df["ORGN"] = df["ORGN"].astype(int)
    df["DEST"] = df["DEST"].astype(int)
    df.insert(0, "year", 2023)
    rows = list(df.itertuples(index=False, name=None))
    execute_values(cur,
        "INSERT INTO od_access_mode (year, orgn, dest, att_aant, att_obus) VALUES %s",
        rows, page_size=2000)
    print(f"  [+] ATTMOD_2023: {len(rows):,}행", flush=True)
    return len(rows)


# ─────────────────────────────────────────────────────────────
# 메인 동기화 함수
# ─────────────────────────────────────────────────────────────
def sync_all(conn, sheet_urls: dict) -> list[dict]:
    """
    모든 테이블 동기화.

    Args:
        conn: psycopg2 connection
        sheet_urls: {"socio": ..., "obj_od": ..., "main_od": ..., "acc_od": ...}

    Returns: 테이블별 적재 리포트
    """
    report = []
    cur = conn.cursor()

    tasks = [
        ("zones",          lambda: load_zones(cur, sheet_urls["socio"])),
        ("socio",          lambda: load_socio(cur, sheet_urls["socio"])),
        ("od_purpose",     lambda: load_od_purpose(cur, sheet_urls["obj_od"])),
        ("od_main_mode",   lambda: load_od_main_mode(cur, sheet_urls["main_od"])),
        ("od_access_mode", lambda: load_od_access_mode(cur, sheet_urls["acc_od"])),
    ]

    for table, fn in tasks:
        print(f"\n[{table}] 동기화 시작...", flush=True)
        t0 = time.time()
        try:
            n = fn()
            dt = time.time() - t0
            cur.execute(
                "INSERT INTO sync_log (table_name, rows_loaded, duration_s) VALUES (%s, %s, %s)",
                (table, n, dt)
            )
            conn.commit()
            print(f"[{table}] 완료: {n:,}행 / {dt:.1f}s", flush=True)
            report.append({"table": table, "rows": n, "duration_s": round(dt, 2)})
        except Exception as e:
            conn.rollback()
            print(f"[{table}] 실패: {e}", flush=True)
            report.append({"table": table, "rows": 0, "duration_s": 0,
                           "error": str(e)})

    cur.close()
    return report


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────
def _cli():
    import tomllib
    with open(".streamlit/secrets.toml", "rb") as f:
        s = tomllib.load(f)

    sb = s["supabase"]
    conn = psycopg2.connect(
        host=sb["host"], port=sb["port"], database=sb["database"],
        user=sb["user"], password=sb["password"], sslmode="require",
    )

    urls = {
        "socio":   s["SHEET_URL_SOCIO"],
        "obj_od":  s["SHEET_URL_OBJ_OD"],
        "main_od": s["SHEET_URL_MAIN_OD"],
        "acc_od":  s["SHEET_URL_ACC_OD"],
    }

    t0 = time.time()
    report = sync_all(conn, urls)
    conn.close()

    total = sum(r.get("rows", 0) for r in report)
    print(f"\n{'='*50}\n전체: {total:,}행 / {time.time()-t0:.1f}s")
    for r in report:
        if "error" in r:
            print(f"  [-] {r['table']}: {r['error']}")


if __name__ == "__main__":
    _cli()
