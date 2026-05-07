"""
KTDB 수도권 데이터 적재 스크립트.

원본자료/*.zip → smr_zones / smr_socio_* / smr_od_* (Azure Postgres ktdb)

설계:
  - TRUNCATE+RELOAD 패턴 (idempotent)
  - 단계 인자: zones / socio / od2023 / od_all / all
  - bulk INSERT: psycopg2.execute_values, page_size=1000
  - sparse drop: OD에서 모든 metric=0 행 제거 (smr_sync_log에 rows_loaded 기록)
  - 인코딩 분기: 사회경제지표(cp949) vs OD(utf-8)
  - DDL/대량 적재는 port 5432 (절대 룰 3, db_env.connect(ddl=True))

실행:
  python sync_smr.py --step zones        # v0: 1310행, ~5초
  python sync_smr.py --step socio        # v1: ~3.2M행, ~3분 (예상)
  python sync_smr.py --step od2023       # v2: 2023년 OD 3종
  python sync_smr.py --step od_all       # v3: 7년 OD 전체
  python sync_smr.py --step all          # 전부 순차 실행

매핑: docs/컬럼매핑표.md 참조.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import zipfile
from pathlib import Path

# Windows cp949 콘솔에서도 한글/em dash가 깨지지 않도록 UTF-8 강제
# + line_buffering=True 로 백그라운드 redirect 시에도 진행 메시지 즉시 출력
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except (AttributeError, OSError):
    pass

import psycopg2
from psycopg2.extras import execute_values

from db_env import connect

REPO_ROOT = Path(__file__).resolve().parent
SOURCES_DIR = REPO_ROOT / "원본자료"
EXTRACT_DIR = REPO_ROOT / ".tmp_extract"

YEARS = [2023, 2025, 2030, 2035, 2040, 2045, 2050]
PURPOSES = ["HOME", "WORK", "SCHO", "OTHE"]


# ════════════════════════════════════════════════════════════════════
# 공통 유틸
# ════════════════════════════════════════════════════════════════════
def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_if_missing(zip_path: Path, suffix_in_zip: str, dest_name: str) -> Path:
    """zip 안 특정 파일을 EXTRACT_DIR/{dest_name}으로 추출 (이미 있으면 skip)."""
    EXTRACT_DIR.mkdir(exist_ok=True)
    out = EXTRACT_DIR / dest_name
    if out.is_file() and out.stat().st_size > 0:
        return out
    with zipfile.ZipFile(zip_path) as zf:
        match = next((n for n in zf.namelist() if n.endswith(suffix_in_zip)), None)
        if match is None:
            raise FileNotFoundError(f"{suffix_in_zip} not in {zip_path.name}")
        with zf.open(match) as src, open(out, "wb") as dst:
            dst.write(src.read())
    return out


def _log_sync(
    cur,
    *,
    dataset: str,
    year: int | None,
    purpose: str | None,
    rows: int,
    duration: float,
    source_md5: str | None = None,
    status: str = "OK",
    error: str | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO smr_sync_log
          (dataset, year, purpose, rows_loaded, duration_s, source_md5, status, error_msg)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (dataset, year, purpose, rows, round(duration, 2), source_md5, status, error),
    )


# ════════════════════════════════════════════════════════════════════
# v0 — zones 적재
# ════════════════════════════════════════════════════════════════════
def _parse_zones_xlsx(xlsx_path: Path) -> list[tuple]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["존체계 양식"]
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # header
        sido, sigu, dong, taz_seq, admin_code, is_inner = row
        if taz_seq is None:
            continue
        rows.append(
            (
                int(taz_seq),
                int(admin_code),
                str(sido) if sido else None,
                str(sigu) if sigu else None,
                str(dong) if dong else None,
                int(is_inner),
            )
        )
    return rows


def step_zones() -> int:
    print("=" * 60)
    print(" v0 — smr_zones 적재")
    print("=" * 60)

    zips = sorted(SOURCES_DIR.glob("*.zip"))
    if not zips:
        print(f"FAIL: zip 파일이 {SOURCES_DIR} 에 없습니다.")
        return 2

    # 어느 zip이든 존체계.xlsx 동일 (공통 자료) — 첫 zip 사용
    xlsx = _extract_if_missing(zips[0], "/존체계.xlsx", "존체계.xlsx")
    md5 = _file_md5(xlsx)
    print(f"  source: {xlsx.name} (md5={md5[:12]}...)")

    rows = _parse_zones_xlsx(xlsx)
    print(f"  parsed: {len(rows)} rows")
    if len(rows) != 1310:
        print(f"  [warn] expected 1310 rows, got {len(rows)}")

    t0 = time.time()
    conn = connect(ddl=True)
    conn.autocommit = False  # 트랜잭션 (TRUNCATE+INSERT 원자성)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE smr_zones CASCADE")
            execute_values(
                cur,
                "INSERT INTO smr_zones(taz_seq, admin_code, sido, sigu, dong, is_inner) VALUES %s",
                rows,
                page_size=500,
            )
            cur.execute("SELECT COUNT(*) FROM smr_zones")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE is_inner=1), "
                "       COUNT(*) FILTER (WHERE is_inner=2), "
                "       COUNT(DISTINCT sido) "
                "FROM smr_zones"
            )
            inner, outer, sido_count = cur.fetchone()
            duration = time.time() - t0
            _log_sync(
                cur,
                dataset="zones",
                year=None,
                purpose=None,
                rows=total,
                duration=duration,
                source_md5=md5,
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()

    print(
        f"  loaded: total={total}, inner={inner}, outer={outer}, "
        f"sido={sido_count}, duration={duration:.2f}s"
    )

    ok = total == 1310 and inner == 1137 and outer == 173 and sido_count == 17
    print(
        f"\n  검증: {'[PASS]' if ok else '[FAIL]'} "
        f"(기대: total=1310, inner=1137, outer=173, sido=17)"
    )
    return 0 if ok else 1


# ════════════════════════════════════════════════════════════════════
# v1 — 사회경제지표 4종 (long format)
# ════════════════════════════════════════════════════════════════════
def _yy(year: int) -> str:
    """2023 → '23'."""
    return f"{year - 2000:02d}"


def _parse_pop_or_emp(path: Path) -> list[tuple[int, str, int, float]]:
    """SUB_POP / EMP_POP — wide 204컬럼을 (taz_seq, gender, age, value) long으로."""
    with open(path, "r", encoding="cp949") as f:
        lines = f.read().splitlines()
    rows: list[tuple[int, str, int, float]] = []
    for ln in lines[1:]:  # 헤더 1줄 skip
        cols = ln.split()
        if len(cols) != 204:
            continue
        taz_seq = int(cols[0])
        # cols[1] = ZONE_ID (admin_code, 검증용; smr_zones FK는 taz_seq 사용)
        # cols[2..102]: 남성 0~100세 (101개)
        for age in range(101):
            v = float(cols[2 + age])
            rows.append((taz_seq, "M", age, v))
        # cols[103..203]: 여성 0~100세 (101개)
        for age in range(101):
            v = float(cols[103 + age])
            rows.append((taz_seq, "F", age, v))
    return rows


def _parse_work(path: Path) -> list[tuple[int, str, float]]:
    """WORK_POP — 4컬럼 (일련, ZONE_ID, 3차, 총종사자)."""
    with open(path, "r", encoding="cp949") as f:
        lines = f.read().splitlines()
    rows: list[tuple[int, str, float]] = []
    for ln in lines[1:]:
        cols = ln.split()
        if len(cols) != 4:
            continue
        taz_seq = int(cols[0])
        rows.append((taz_seq, "3RD ", float(cols[2])))
        rows.append((taz_seq, "TOT ", float(cols[3])))
    return rows


def _parse_stu(path: Path) -> list[tuple[int, str, float]]:
    """STU_POP — 7컬럼 (일련, ZONE_ID, 초, 중, 고, 특수, 대)."""
    levels = ["ELEM", "MID ", "HIGH", "SPEC", "UNIV"]
    with open(path, "r", encoding="cp949") as f:
        lines = f.read().splitlines()
    rows: list[tuple[int, str, float]] = []
    for ln in lines[1:]:
        cols = ln.split()
        if len(cols) != 7:
            continue
        taz_seq = int(cols[0])
        for i, lvl in enumerate(levels):
            rows.append((taz_seq, lvl, float(cols[2 + i])))
    return rows


def _load_socio_indicator(
    conn,
    *,
    table: str,
    file_pattern: str,
    parser,
    has_dim: bool,  # True=(taz, code, value), False=(taz, gender, age, value)
    src_zip: Path,
) -> tuple[int, str]:
    """단일 사회경제지표 indicator 적재. 반환: (rows_loaded, combined_md5)."""
    all_rows: list[tuple] = []
    md5_combined = hashlib.md5()

    for year in YEARS:
        fname = file_pattern.format(yy=_yy(year))
        path = _extract_if_missing(src_zip, f"/{fname}", fname)
        md5_combined.update(_file_md5(path).encode())
        parsed = parser(path)
        for r in parsed:
            all_rows.append((year, *r))

    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {table}")
        if has_dim:
            sql = f"INSERT INTO {table}(year, taz_seq, gender, age, value) VALUES %s"
        else:
            # work / stu: (year, taz_seq, indicator|level, value)
            sql = f"INSERT INTO {table}(year, taz_seq, {'indicator' if 'work' in table else 'level'}, value) VALUES %s"
        execute_values(cur, sql, all_rows, page_size=2000)
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        cnt = cur.fetchone()[0]
        duration = time.time() - t0
        _log_sync(
            cur,
            dataset=table.replace("smr_", ""),
            year=None,
            purpose=None,
            rows=cnt,
            duration=duration,
            source_md5=md5_combined.hexdigest(),
        )
    return cnt, md5_combined.hexdigest()


def step_socio() -> int:
    print("=" * 60)
    print(" v1 — 사회경제지표 4종 (long format) 적재")
    print("=" * 60)

    zips = sorted(SOURCES_DIR.glob("*.zip"))
    if not zips:
        print(f"FAIL: zip 파일이 {SOURCES_DIR} 에 없습니다.")
        return 2
    src_zip = zips[0]
    print(f"  source zip: {src_zip.name}")

    indicators = [
        ("smr_socio_pop", "SUB_POP{yy}.TXT", _parse_pop_or_emp, True, "인구수"),
        ("smr_socio_emp", "EMP_POP_{yy}.TXT", _parse_pop_or_emp, True, "취업자수"),
        ("smr_socio_work", "WORK_POP{yy}.TXT", _parse_work, False, "종사자수"),
        ("smr_socio_stu", "STU_POP{yy}.TXT", _parse_stu, False, "학생수"),
    ]

    overall_t0 = time.time()
    conn = connect(ddl=True)
    conn.autocommit = False
    try:
        results = []
        for table, pattern, parser, has_dim, label in indicators:
            print(f"\n  → {label} ({table})")
            t0 = time.time()
            cnt, md5 = _load_socio_indicator(
                conn,
                table=table,
                file_pattern=pattern,
                parser=parser,
                has_dim=has_dim,
                src_zip=src_zip,
            )
            duration = time.time() - t0
            print(f"    rows={cnt:,}, duration={duration:.1f}s")
            results.append((label, table, cnt, duration))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()

    overall = time.time() - overall_t0
    print(f"\n  전체 duration: {overall:.1f}s")
    print(f"  총 적재: {sum(r[2] for r in results):,} 행")

    # 기댓값 검증 (1137 권역내부 zones × per-indicator dim × 7년)
    expected = {
        "smr_socio_pop": 1137 * 202 * 7,  # M+F × 0~100 × 7yr
        "smr_socio_emp": 1137 * 202 * 7,
        "smr_socio_work": 1137 * 2 * 7,  # 3RD + TOT × 7yr
        "smr_socio_stu": 1137 * 5 * 7,   # 5 levels × 7yr
    }
    print("\n  검증:")
    all_ok = True
    for label, table, cnt, _dur in results:
        exp = expected[table]
        ok = cnt == exp
        all_ok &= ok
        sign = "[PASS]" if ok else "[WARN]"
        print(f"    {sign} {table}: {cnt:,} (기대 {exp:,})")
    return 0 if all_ok else 1


# ════════════════════════════════════════════════════════════════════
# v2/v3 — OD 적재 (sparse drop, partition routing)
# ════════════════════════════════════════════════════════════════════
def _find_zip(name_substring: str) -> Path:
    for z in sorted(SOURCES_DIR.glob("*.zip")):
        if name_substring in z.name:
            return z
    raise FileNotFoundError(f"zip with {name_substring!r} not found in {SOURCES_DIR}")


def _parse_od_file(path: Path, n_metrics: int) -> tuple[list[tuple], int, int]:
    """OD 파일 → [(orgn_seq, dest_seq, *metrics)]. sparse(all-zero) 제거.
    반환: (rows, total_lines, kept_lines)"""
    rows: list[tuple] = []
    total = 0
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip()
            if not ln:
                continue
            total += 1
            cols = ln.split()
            if len(cols) != 4 + n_metrics:
                continue
            orgn = int(cols[0])
            dest = int(cols[2])
            metrics = [float(v) for v in cols[4 : 4 + n_metrics]]
            if not any(v != 0.0 for v in metrics):
                continue  # sparse drop
            rows.append((orgn, dest, *metrics))
    return rows, total, len(rows)


def _bulk_insert_od(
    cur,
    *,
    parent_table: str,
    partition_table: str,
    year: int,
    purpose: str | None,
    metric_cols: list[str],
    rows: list[tuple],
) -> int:
    """파티션 TRUNCATE + 부모 테이블 INSERT (자동 partition 라우팅)."""
    # purpose가 있는 OD는 partition 단일 (year)이지만 같은 파티션에 4개 purpose가 함께 — 첫 호출에만 TRUNCATE
    # 호출자가 같은 (year, purpose) 조합당 1회 TRUNCATE 책임 (단순화: 매번 DELETE WHERE)
    if purpose is None:
        cur.execute(f"TRUNCATE {partition_table}")
    else:
        # purpose 단위로 정확히 비우기 (다른 purpose 보존)
        cur.execute(
            f"DELETE FROM {parent_table} WHERE year=%s AND purpose=%s",
            (year, purpose),
        )

    if purpose is None:
        cols_sql = "year, orgn_seq, dest_seq, " + ", ".join(metric_cols)
        rows_with_year = [(year, *r) for r in rows]
    else:
        cols_sql = "year, purpose, orgn_seq, dest_seq, " + ", ".join(metric_cols)
        rows_with_year = [(year, purpose, *r) for r in rows]

    sql = f"INSERT INTO {parent_table}({cols_sql}) VALUES %s"
    execute_values(cur, sql, rows_with_year, page_size=2000)
    cur.execute(
        f"SELECT COUNT(*) FROM {parent_table} WHERE year=%s"
        + (" AND purpose=%s" if purpose else ""),
        ((year, purpose) if purpose else (year,)),
    )
    return cur.fetchone()[0]


PURPOSE_OD_COLS = ["home", "work", "scho", "busi", "othe"]
MAIN_MODE_COLS = [
    "walk_bike", "freight", "etc_bus", "rail", "ktx",
    "auto", "taxi", "bus", "subw", "bus_subw",
]


def _load_od_purpose(conn, year: int, src_zip: Path) -> tuple[int, int, float]:
    fname = f"ODTRIP{_yy(year)}_F.OUT"
    path = _extract_if_missing(src_zip, f"/{fname}", fname)
    md5 = _file_md5(path)
    t0 = time.time()
    rows, total, kept = _parse_od_file(path, n_metrics=5)
    print(f"      parse: {fname} total={total:,}, kept={kept:,} ({kept/total*100:.1f}%)")
    with conn.cursor() as cur:
        loaded = _bulk_insert_od(
            cur,
            parent_table="smr_od_purpose",
            partition_table=f"smr_od_purpose_y{year}",
            year=year,
            purpose=None,
            metric_cols=PURPOSE_OD_COLS,
            rows=rows,
        )
        duration = time.time() - t0
        _log_sync(
            cur, dataset="od_purpose", year=year, purpose=None,
            rows=loaded, duration=duration, source_md5=md5,
        )
    return total, loaded, duration


def _load_od_main_mode(conn, year: int, src_zip: Path) -> tuple[int, int, float]:
    fname = f"OD_MMODE_{_yy(year)}_F.TXT"
    path = _extract_if_missing(src_zip, f"/{fname}", fname)
    md5 = _file_md5(path)
    t0 = time.time()
    rows, total, kept = _parse_od_file(path, n_metrics=10)
    print(f"      parse: {fname} total={total:,}, kept={kept:,} ({kept/total*100:.1f}%)")
    with conn.cursor() as cur:
        loaded = _bulk_insert_od(
            cur,
            parent_table="smr_od_main_mode",
            partition_table=f"smr_od_main_mode_y{year}",
            year=year,
            purpose=None,
            metric_cols=MAIN_MODE_COLS,
            rows=rows,
        )
        duration = time.time() - t0
        _log_sync(
            cur, dataset="od_main_mode", year=year, purpose=None,
            rows=loaded, duration=duration, source_md5=md5,
        )
    return total, loaded, duration


def _load_od_purpose_mode(conn, year: int, purpose: str, src_zip: Path) -> tuple[int, int, float]:
    fname = f"OD_MMODE_{purpose}_{_yy(year)}_F.TXT"
    path = _extract_if_missing(src_zip, f"/{fname}", fname)
    md5 = _file_md5(path)
    t0 = time.time()
    rows, total, kept = _parse_od_file(path, n_metrics=10)
    print(f"      parse: {fname} total={total:,}, kept={kept:,} ({kept/total*100:.1f}%)")
    with conn.cursor() as cur:
        loaded = _bulk_insert_od(
            cur,
            parent_table="smr_od_purpose_mode",
            partition_table=f"smr_od_purpose_mode_y{year}",
            year=year,
            purpose=purpose,
            metric_cols=MAIN_MODE_COLS,
            rows=rows,
        )
        duration = time.time() - t0
        _log_sync(
            cur, dataset="od_purpose_mode", year=year, purpose=purpose,
            rows=loaded, duration=duration, source_md5=md5,
        )
    return total, loaded, duration


def _od_one_year(conn, year: int, *, zip_obj: Path, zip_mod: Path, zip_purmod: Path) -> dict:
    print(f"\n  >>>>> {year}년 OD 3종 적재 <<<<<")
    summary = {}

    print(f"    → 목적OD")
    summary["od_purpose"] = _load_od_purpose(conn, year, zip_obj)
    print(f"    → 주수단OD")
    summary["od_main_mode"] = _load_od_main_mode(conn, year, zip_mod)
    print(f"    → 목적별주수단OD (4목적)")
    pm_results = {}
    for p in PURPOSES:
        pm_results[p] = _load_od_purpose_mode(conn, year, p, zip_purmod)
    summary["od_purpose_mode"] = pm_results

    return summary


def _print_year_summary(year: int, summary: dict) -> None:
    p_total, p_kept, p_dur = summary["od_purpose"]
    m_total, m_kept, m_dur = summary["od_main_mode"]
    print(f"\n  {year}년 적재 결과:")
    print(f"    smr_od_purpose       : {p_kept:>10,} / {p_total:>10,} ({p_kept/p_total*100:5.1f}%) {p_dur:6.1f}s")
    print(f"    smr_od_main_mode     : {m_kept:>10,} / {m_total:>10,} ({m_kept/m_total*100:5.1f}%) {m_dur:6.1f}s")
    pm_total = sum(v[1] for v in summary["od_purpose_mode"].values())
    pm_raw = sum(v[0] for v in summary["od_purpose_mode"].values())
    pm_dur = sum(v[2] for v in summary["od_purpose_mode"].values())
    print(f"    smr_od_purpose_mode  : {pm_total:>10,} / {pm_raw:>10,} ({pm_total/pm_raw*100:5.1f}%) {pm_dur:6.1f}s (4 purposes)")


def step_od_2023() -> int:
    print("=" * 60)
    print(" v2 — OD 2023년 1년 적재 (sparse drop 검증)")
    print("=" * 60)
    return _step_od_years([2023])


def step_od_all() -> int:
    print("=" * 60)
    print(f" v3 — OD 7년 풀 적재 ({YEARS[0]}~{YEARS[-1]})")
    print("=" * 60)
    return _step_od_years(YEARS)


def _step_od_years(years: list[int]) -> int:
    try:
        zip_obj = _find_zip("OBJ-01")
        zip_mod = _find_zip("MOD-11")
        zip_purmod = _find_zip("MOD-21")
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 2
    print(f"  zips: {zip_obj.name} / {zip_mod.name} / {zip_purmod.name}")

    overall_t0 = time.time()
    conn = connect(ddl=True)
    conn.autocommit = False
    try:
        for year in years:
            summary = _od_one_year(
                conn, year, zip_obj=zip_obj, zip_mod=zip_mod, zip_purmod=zip_purmod
            )
            conn.commit()  # 연도별 commit (실패 시 부분 보존)
            _print_year_summary(year, summary)
    except Exception as e:
        conn.rollback()
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()

    overall = time.time() - overall_t0
    print(f"\n  전체 duration: {overall/60:.1f}분 ({overall:.0f}s)")
    return 0


# ════════════════════════════════════════════════════════════════════
# entrypoint
# ════════════════════════════════════════════════════════════════════
STEPS = {
    "zones": step_zones,
    "socio": step_socio,
    "od2023": step_od_2023,
    "od_all": step_od_all,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="KTDB 수도권 데이터 적재")
    parser.add_argument(
        "--step",
        choices=list(STEPS.keys()) + ["all"],
        default="zones",
        help="실행 단계 (default: zones)",
    )
    args = parser.parse_args()

    if args.step == "all":
        for name, fn in STEPS.items():
            print(f"\n>>>>> {name} <<<<<")
            rc = fn()
            if rc != 0:
                return rc
        return 0
    return STEPS[args.step]()


if __name__ == "__main__":
    sys.exit(main())
