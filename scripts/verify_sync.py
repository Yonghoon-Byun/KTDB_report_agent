"""
적재 결과 검증 스크립트 — sync_smr.py 실행 후 일관성 점검.

사용:
  python scripts/verify_sync.py            # 전체 검증
  python scripts/verify_sync.py --quick    # 행 수 + 최근 sync_log만
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# UTF-8 콘솔 강제 (Windows cp949 호환)
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except (AttributeError, OSError):
    pass

# 프로젝트 루트를 sys.path에 추가 (db_env import)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_env import connect


# 단계별 기댓값 (sparse drop 후, 7년 기준)
EXPECTED = {
    "smr_zones": 1310,
    "smr_socio_pop": 1137 * 202 * 7,
    "smr_socio_emp": 1137 * 202 * 7,
    "smr_socio_work": 1137 * 2 * 7,
    "smr_socio_stu": 1137 * 5 * 7,
    "smr_od_purpose": None,         # sparse 의존, 추정 ~10.85M
    "smr_od_main_mode": None,        # 추정 ~10.85M
    "smr_od_purpose_mode": None,     # 추정 ~28.84M
}


def section(title: str) -> None:
    print(f"\n{'═' * 70}")
    print(f"  {title}")
    print("═" * 70)


def check_table_counts(cur, *, quick: bool = False) -> int:
    """모든 smr_* 테이블의 행 수 + 기댓값 비교."""
    section("테이블별 행 수")
    print(f"  {'table':<25} {'actual':>15} {'expected':>15} {'status':>8}")
    print(f"  {'-' * 25} {'-' * 15} {'-' * 15} {'-' * 8}")
    fail = 0
    for table, exp in EXPECTED.items():
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        actual = cur.fetchone()[0]
        if exp is None:
            status = f"{'(?)' if actual == 0 else '[PASS]'}"
        else:
            status = "[PASS]" if actual == exp else "[FAIL]"
            if actual != exp:
                fail += 1
        exp_str = f"{exp:,}" if exp is not None else "—"
        print(f"  {table:<25} {actual:>15,} {exp_str:>15} {status:>8}")
    return fail


def check_sync_log(cur, limit: int = 20) -> None:
    section(f"smr_sync_log 최근 {limit}개")
    cur.execute(
        """
        SELECT dataset, year, purpose, rows_loaded, duration_s, status, synced_at
        FROM smr_sync_log
        ORDER BY synced_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    if not rows:
        print("  (no entries)")
        return
    print(f"  {'dataset':<22} {'year':>5} {'purpose':>8} {'rows':>12} {'sec':>7} {'status':>6}  synced_at")
    print(f"  {'-' * 22} {'-' * 5} {'-' * 8} {'-' * 12} {'-' * 7} {'-' * 6}  {'-' * 19}")
    for ds, yr, p, rows_loaded, dur, status, ts in rows:
        yr_str = str(yr) if yr is not None else "—"
        p_str = p.strip() if p else "—"
        print(f"  {ds:<22} {yr_str:>5} {p_str:>8} {rows_loaded:>12,} {dur:>7.1f} {status:>6}  {ts.strftime('%Y-%m-%d %H:%M:%S')}")


def check_od_year_distribution(cur) -> None:
    section("연도별 OD 분포")
    for table in ("smr_od_purpose", "smr_od_main_mode"):
        cur.execute(f"SELECT year, COUNT(*) FROM {table} GROUP BY year ORDER BY year")
        rows = cur.fetchall()
        if not rows:
            print(f"  {table}: (empty)")
            continue
        print(f"  {table}:")
        for yr, cnt in rows:
            print(f"    {yr}: {cnt:>10,}")

    # purpose_mode는 (year, purpose) 분포
    cur.execute(
        "SELECT year, purpose, COUNT(*) FROM smr_od_purpose_mode "
        "GROUP BY year, purpose ORDER BY year, purpose"
    )
    rows = cur.fetchall()
    if rows:
        print(f"  smr_od_purpose_mode:")
        for yr, p, cnt in rows:
            print(f"    {yr} {p.strip():4}: {cnt:>10,}")


def check_zones_integrity(cur) -> int:
    section("smr_zones 무결성")
    cur.execute("SELECT COUNT(*) FILTER (WHERE is_inner=1), COUNT(*) FILTER (WHERE is_inner=2) FROM smr_zones")
    inner, outer = cur.fetchone()
    cur.execute("SELECT COUNT(DISTINCT sido) FROM smr_zones")
    sido = cur.fetchone()[0]
    fail = 0
    print(f"  권역내부: {inner:>6,} (기대 1137) {'[PASS]' if inner == 1137 else '[FAIL]'}")
    print(f"  권역외부: {outer:>6,} (기대  173) {'[PASS]' if outer == 173 else '[FAIL]'}")
    print(f"  시도 수 : {sido:>6,} (기대   17) {'[PASS]' if sido == 17 else '[FAIL]'}")
    fail += sum([inner != 1137, outer != 173, sido != 17])
    return fail


def main() -> int:
    parser = argparse.ArgumentParser(description="KTDB sync 결과 검증")
    parser.add_argument("--quick", action="store_true", help="행 수 + sync_log 만")
    args = parser.parse_args()

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), version()")
            db, ver = cur.fetchone()
            print(f"connected: {db} ({ver.split(',')[0]})")

            fail = 0
            fail += check_table_counts(cur, quick=args.quick)
            check_sync_log(cur, limit=20)
            if not args.quick:
                check_od_year_distribution(cur)
                fail += check_zones_integrity(cur)

        section("종합 결과")
        if fail == 0:
            print("  [PASS] 모든 검증 통과 (sparse drop 의존 OD는 sync_log로 별도 확인)")
            return 0
        else:
            print(f"  [FAIL] {fail}개 항목 실패")
            return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
