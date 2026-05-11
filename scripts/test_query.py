"""백엔드 단 query 테스트 — Streamlit UI 없이 단계별 시간 측정.

사용법:
    python scripts/test_query.py "장래에 수도권 시군구 중 일반철도와 KTX 수단 비율이 높아지는 지역은?"
    python scripts/test_query.py --backend azure "2030년 경기도 인구수"
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit_app as app  # noqa: E402


def _t(label: str, start: float) -> float:
    now = time.perf_counter()
    print(f"  [{now - start:6.2f}s] {label}")
    return now


def run(query: str, backend: str = "azure") -> None:
    print(f"\n=== Query ===\n{query}\n=== Backend: {backend} ===\n")
    t0 = time.perf_counter()

    chat_years = app.extract_years_from_query(query)
    target_years = sorted(set(chat_years))
    agg_level = app.detect_aggregation(query)
    top_n = app.detect_top_n(query)
    (o_sido, o_sigu), (d_sido, d_sigu) = app.extract_od_pair_from_query(backend, query)
    origin_scope, dest_scope = app.detect_region_scope(query)
    exclude_self_dest = app.detect_exclude_self_dest(query)
    print("[Routing]")
    print(f"  years              = {target_years}")
    print(f"  agg_level          = {agg_level}")
    print(f"  top_n              = {top_n}")
    print(f"  origin             = sido={o_sido!r} sigu={o_sigu!r} scope={origin_scope!r}")
    print(f"  dest               = sido={d_sido!r} sigu={d_sigu!r} scope={dest_scope!r}")
    print(f"  exclude_self_dest  = {exclude_self_dest}")

    combos = app.auto_route(query, backend, target_years)
    print(f"  combos ({len(combos)}):")
    for f, t in combos:
        print(f"    - {f} > {t}")

    def _decide() -> str | None:
        if agg_level == "도착시군구":
            return "dest_sigu"
        if agg_level == "도착시도":
            return "dest_sido"
        if d_sido or d_sigu or dest_scope:
            return None
        if agg_level == "시군구":
            return "origin_sigu"
        if agg_level == "시도":
            return "origin_sido"
        return None

    od_pre_agg = _decide()
    print(f"  od_pre_agg     = {od_pre_agg}")

    print("\n[Load timings]")
    last = time.perf_counter()
    datasets = []
    for file_label, tab in combos:
        pre = od_pre_agg if file_label != "사회경제지표" else None
        df, _interp = app.load_integrated(
            backend,
            file_label,
            tab,
            target_years,
            query,
            sido_sel=o_sido or "전체",
            sigu_sel=o_sigu or "전체",
            dest_sido_sel=d_sido or "전체",
            dest_sigu_sel=d_sigu or "전체",
            origin_scope=origin_scope,
            dest_scope=dest_scope,
            pre_aggregate_level=pre,
            exclude_self_dest=exclude_self_dest,
        )
        df_disp = app.aggregate_by_region(df, agg_level) if agg_level else df
        datasets.append({"file": file_label, "tab": tab,
                         "tab_kr": app.get_dataset_config(backend)[file_label]["tabs"][tab],
                         "df": df, "df_for_display": df_disp})
        last = _t(f"{file_label} > {tab}  rows={len(df)}", last)

    print("\n[Build tables]")
    tables = app.build_result_tables(datasets, query, target_years)
    last = _t(f"build_result_tables ({len(tables)} tables)", last)

    for tb in tables:
        tb["df"] = app.add_ratio_columns(tb["df"], query)
        tb["df"] = app.add_change_columns(tb["df"], query)
        tb["df"] = app.add_cagr_columns(tb["df"], query)
        tb["df"], _u = app.apply_unit_scale(tb["df"], query)
    last = _t("post-process (ratio/change/cagr/unit)", last)

    if top_n:
        tables = app.apply_top_n(tables, top_n, query)
        last = _t(f"apply_top_n({top_n})", last)

    total = time.perf_counter() - t0
    print(f"\n[TOTAL] {total:.2f}s\n")

    print("[Result tables]")
    for i, tb in enumerate(tables):
        df = tb["df"]
        print(f"\n--- ({i+1}) {tb['title']}  shape={df.shape}  unit={tb.get('unit', '')}")
        if tb.get("note"):
            print(f"    note: {tb['note']}")
        with __import__("pandas").option_context("display.max_columns", 30,
                                                  "display.width", 220):
            print(df.head(20).to_string(index=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--backend", default="azure", choices=["azure", "legacy"])
    args = p.parse_args()
    run(args.query, args.backend)
