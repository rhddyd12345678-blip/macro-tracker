"""
한국 금리 링크 회귀 분석 (SPEC §5).

    Δ국고3Y(bp) ~ β1·ΔUST2Y + β2·ΔBund2Y + β3·ΔJGB10Y + β4·ΔUSDKRW

Bund2Y/JGB10Y는 아직 id_verified=N이라 REGRESSORS에서 뺐다. 데이터가 확보되면
아래 REGRESSORS 리스트에 한 줄만 추가하면 되고, 회귀/롤링/lead-lag 로직은
전부 regressor 개수에 무관하게 동작한다.

각 시리즈는 kind에 따라 Δ를 다르게 계산한다:
  - "rate_bp"   : (오늘값 - 어제값) * 100  (금리, 퍼센트 포인트 -> bp)
  - "fx_logret" : log(오늘값/어제값) * 100 (환율, 로그수익률 %)
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DB_PATH

DEPENDENT = {"id": "kr_ktb3y", "label": "KTB3Y", "kind": "rate_bp"}

REGRESSORS = [
    {"id": "us_2y", "label": "UST2Y", "kind": "rate_bp"},
    {"id": "usdkrw", "label": "USDKRW", "kind": "fx_logret"},
    # 나중에 추가할 때:
    # {"id": "de_bund2y", "label": "Bund2Y", "kind": "rate_bp"},
    # {"id": "jp_jgb10y", "label": "JGB10Y", "kind": "rate_bp"},
]

ROLLING_WINDOWS = [60, 120]
LEAD_LAG_MAX_DAYS = 10
ANALYSIS_YEARS = 5          # 결과로 내보내는 표시 구간
MIN_LEAD_LAG_SAMPLE = 60    # lag별 상관계수 계산에 필요한 최소 표본


def load_series_dict(conn, series_id):
    cur = conn.execute(
        "SELECT date, value FROM series_daily WHERE series_id = ? ORDER BY date",
        (series_id,),
    )
    return {d: v for d, v in cur.fetchall() if v is not None}


def diff_value(kind, prev_val, cur_val):
    if kind == "rate_bp":
        return (cur_val - prev_val) * 100.0
    if kind == "fx_logret":
        if prev_val <= 0 or cur_val <= 0:
            return None
        return float(np.log(cur_val / prev_val) * 100.0)
    raise ValueError(f"지원하지 않는 kind: {kind}")


def pearson(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def ols_with_r2(X, y):
    coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    y_hat = X @ coef
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return coef, r_squared


def build_aligned_diffs(conn):
    """의존/설명 변수를 공통 거래일에 맞춰 정렬하고 Δ를 계산.

    반환: (diff_dates, y, {regressor_id: x_array})
    """
    dep_series = load_series_dict(conn, DEPENDENT["id"])
    reg_series = {r["id"]: load_series_dict(conn, r["id"]) for r in REGRESSORS}

    common_dates = set(dep_series)
    for s in reg_series.values():
        common_dates &= set(s)
    aligned = sorted(common_dates)

    if len(aligned) < 2:
        return [], np.array([]), {r["id"]: np.array([]) for r in REGRESSORS}

    diff_dates, y_list = [], []
    x_lists = {r["id"]: [] for r in REGRESSORS}

    for prev_d, cur_d in zip(aligned[:-1], aligned[1:]):
        dy = diff_value(DEPENDENT["kind"], dep_series[prev_d], dep_series[cur_d])
        row_x = {}
        ok = dy is not None
        for r in REGRESSORS:
            dx = diff_value(r["kind"], reg_series[r["id"]][prev_d], reg_series[r["id"]][cur_d])
            row_x[r["id"]] = dx
            ok = ok and dx is not None
        if not ok:
            continue
        diff_dates.append(cur_d)
        y_list.append(dy)
        for rid, dx in row_x.items():
            x_lists[rid].append(dx)

    y = np.array(y_list, dtype=float)
    x_arrays = {rid: np.array(vals, dtype=float) for rid, vals in x_lists.items()}
    return diff_dates, y, x_arrays


def rolling_regression(diff_dates, y, x_arrays, window):
    keys = [r["id"] for r in REGRESSORS]
    n = len(y)
    results = []
    for i in range(window, n + 1):
        sl = slice(i - window, i)
        y_win = y[sl]
        X_win = np.column_stack([np.ones(window)] + [x_arrays[k][sl] for k in keys])
        coef, r_squared = ols_with_r2(X_win, y_win)
        row = {"date": diff_dates[i - 1], "r_squared": r_squared}
        for j, r in enumerate(REGRESSORS):
            row[f"beta_{r['id']}"] = float(coef[j + 1])
            row[f"corr_{r['id']}"] = pearson(y_win, x_arrays[r["id"]][sl])
        results.append(row)
    return results


def lead_lag_correlation(y, x, max_lag):
    """lag > 0: x가 y를 며칠 선행하는가 (x[t-lag] vs y[t])."""
    n = len(y)
    out = []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a, b = y[lag:], x[: n - lag] if lag > 0 else x
        else:
            a, b = y[: n + lag], x[-lag:]
        if len(a) < MIN_LEAD_LAG_SAMPLE:
            continue
        out.append({"lag": lag, "corr": pearson(a, b), "n": len(a)})
    return out


def best_lag(lead_lag_rows):
    valid = [r for r in lead_lag_rows if r["corr"] is not None]
    if not valid:
        return None
    return max(valid, key=lambda r: abs(r["corr"]))


def trim_to_years(rows, years):
    if not rows:
        return rows
    cutoff = f"{int(rows[-1]['date'][:4]) - years}{rows[-1]['date'][4:]}"
    return [r for r in rows if r["date"] >= cutoff]


def run_kr_link_analysis(conn):
    diff_dates, y, x_arrays = build_aligned_diffs(conn)

    if len(y) < max(ROLLING_WINDOWS) + 1:
        return {
            "dependent": DEPENDENT["label"],
            "regressors": [r["label"] for r in REGRESSORS],
            "error": "정렬된 공통 거래일 데이터가 부족합니다",
            "n": len(y),
        }

    rolling = {
        f"{w}d": trim_to_years(rolling_regression(diff_dates, y, x_arrays, w), ANALYSIS_YEARS)
        for w in ROLLING_WINDOWS
    }

    recent_cutoff_idx = 0
    cutoff_year = int(diff_dates[-1][:4]) - ANALYSIS_YEARS
    for i, d in enumerate(diff_dates):
        if int(d[:4]) >= cutoff_year:
            recent_cutoff_idx = i
            break

    lead_lag = {}
    for r in REGRESSORS:
        rows = lead_lag_correlation(
            y[recent_cutoff_idx:], x_arrays[r["id"]][recent_cutoff_idx:], LEAD_LAG_MAX_DAYS
        )
        lead_lag[r["id"]] = {"label": r["label"], "rows": rows, "best": best_lag(rows)}

    return {
        "dependent": DEPENDENT["label"],
        "regressors": [{"id": r["id"], "label": r["label"]} for r in REGRESSORS],
        "sample": {"start": diff_dates[0], "end": diff_dates[-1], "n": len(diff_dates)},
        "rolling": rolling,
        "lead_lag": lead_lag,
        "note": "Bund2Y/JGB10Y는 id_verified=Y 확보 전까지 제외. "
                "REGRESSORS 리스트에 추가하면 자동으로 회귀에 포함됨.",
    }


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    result = run_kr_link_analysis(conn)
    conn.close()

    if "error" in result:
        print("분석 실패:", result["error"], "(n =", result["n"], ")")
    else:
        print(f"표본: {result['sample']['start']} ~ {result['sample']['end']} ({result['sample']['n']}일)")
        for w, rows in result["rolling"].items():
            if rows:
                last = rows[-1]
                print(f"[{w}] 최신 베타:", {k: v for k, v in last.items() if k != "date"})
        for rid, ll in result["lead_lag"].items():
            print(f"[lead-lag {ll['label']}] best:", ll["best"])
