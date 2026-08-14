#!/usr/bin/env python3
"""Autonomous slice discovery: let the data propose the rules.

Every prior attempt failed because a HUMAN chose the mechanism (first
pullback, ORB5, gap-hold) and the data merely declined to confirm it. This
inverts that: compute descriptive state features, enumerate every
combination of them, and let the search surface which conditions actually
precede positive forward returns.

THE DANGER, AND THE WHOLE POINT OF THIS FILE
--------------------------------------------
Searching thousands of slices GUARANTEES false positives. Testing 4,500
slices at p<0.05 yields ~225 "winners" from pure noise. That is almost
exactly what the "9,000 backtests -> 524 survivors" video produced, and it
is why its survivors should not be trusted: 9,000 x 0.05 = 450.

So this engine is built around the correction, not the search:

  1. THREE-WAY TIME SPLIT. Discovery runs on train only. Validation
     filters. The holdout is touched ONCE, at the end, and never informs
     any choice.
  2. BENJAMINI-HOCHBERG FDR across every hypothesis tested. Reported
     alongside the raw count so the noise floor is always visible.
  3. EXPECTED-BY-CHANCE baseline printed next to observed survivors. If
     survivors ~ expected, the answer is "nothing found" no matter how
     good the best slice looks.
  4. MINIMUM SAMPLE per slice, and R-multiples not percentages (percent
     scales with volatility; R is what compounds under risk-parity sizing).
  5. LABEL SHUFFLE control: rerun the entire search on shuffled outcomes.
     Survivors there are pure noise, and give an empirical false-positive
     rate to compare against.

Usage:
  PYTHONPATH=src python3 scripts/discover.py
  PYTHONPATH=src python3 scripts/discover.py --max-depth 3 --min-n 100
  PYTHONPATH=src python3 scripts/discover.py --shuffle-control
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# Liquid universe: chosen so the result is AFFORDABLE to validate. The
# microcap closure showed ~1,900 trades needed for a 1% edge at 15pp
# dispersion; liquid names cost 3-10bps and disperse far less.
UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "USO", "XLK", "XLF", "XLE",
    "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "EFA", "EEM", "HYG", "LQD",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "V", "UNH", "HD",
    "ABBV", "TMO", "AVGO", "MCD", "NKE", "WMT", "DIS", "BA", "CAT", "HON",
]
COST_BPS = 3.0
ATR_STOP_MULT = 1.5
RR_TARGET = 2.0
HOLD_BARS = 10


def load(symbol, period="15y"):
    import yfinance as yf
    try:
        raw = yf.download(symbol, period=period, interval="1d",
                          progress=False, auto_adjust=True)
    except Exception:
        return None
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    tcol = next((c for c in ("Date", "Datetime", "index") if c in raw.columns), None)
    if tcol is None:
        return None
    out = pd.DataFrame({
        "ts": pd.to_datetime(raw[tcol]).dt.tz_localize(None),
        "o": raw["Open"].astype(float), "h": raw["High"].astype(float),
        "l": raw["Low"].astype(float), "c": raw["Close"].astype(float),
        "v": raw["Volume"].astype(float),
    })
    return out[out["v"] > 0].reset_index(drop=True)


def features(df):
    """Descriptive state only. No strategy opinion encoded here."""
    f = pd.DataFrame(index=df.index)
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]

    tr = np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
    atr = tr.rolling(14).mean()
    f["atr"] = atr

    sma20 = c.rolling(20).mean()
    sma50 = c.rolling(50).mean()
    sma200 = c.rolling(200).mean()

    # ATR-normalised extension from the 20-day mean
    f["ext"] = (c - sma20) / atr
    # Regime
    f["regime"] = np.where(sma50 > sma200, "up", "down")
    # Trend slope, normalised
    f["slope"] = (sma20 - sma20.shift(10)) / atr
    # RSI
    d = c.diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean()
    f["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    # Realised vol regime, relative to its own year
    rv = c.pct_change().rolling(20).std()
    f["volrank"] = rv.rolling(252).rank(pct=True)
    # Recent returns
    f["ret5"] = c.pct_change(5) / (atr / c)
    f["ret1"] = c.pct_change(1) / (atr / c)
    # Volume surge
    f["volratio"] = v / v.rolling(20).mean()
    # Distance to 52w high
    f["dd52"] = (c / c.rolling(252).max() - 1.0)
    # Calendar
    f["dow"] = df["ts"].dt.dayofweek
    f["month"] = df["ts"].dt.month

    # --- kitchen sink: everything cheap and descriptive ---
    # Overnight gap, ATR-normalised
    f["gap"] = (df["o"] - c.shift()) / atr
    # Where the bar closed within its own range (0 = on low, 1 = on high)
    rng_ = (h - l).replace(0, np.nan)
    f["clpos"] = (c - l) / rng_
    # Bar range vs typical range
    f["rangeratio"] = (h - l) / atr
    # Consecutive up / down closes
    up1 = (c > c.shift()).astype(int)
    streak = up1.groupby((up1 != up1.shift()).cumsum()).cumcount() + 1
    f["streak_up"] = np.where(up1 == 1, streak, 0)
    f["streak_dn"] = np.where(up1 == 0, streak, 0)
    # Distance from longer means
    f["ext50"] = (c - sma50) / atr
    f["ext200"] = (c - sma200) / atr
    # Volatility direction: is vol expanding or contracting
    rv2 = c.pct_change().rolling(20).std()
    f["volchg"] = (rv2 / rv2.shift(20)) - 1.0
    # Relative strength vs SPY over 20 bars (filled later, needs market)
    f["ret20"] = c.pct_change(20) / (atr / c)
    # Position within the 52-week range
    hi52 = c.rolling(252).max()
    lo52 = c.rolling(252).min()
    f["pos52"] = (c - lo52) / (hi52 - lo52).replace(0, np.nan)
    # Volume trend
    f["voltrend"] = v.rolling(5).mean() / v.rolling(50).mean()
    # Days since a 20-day high
    f["since_high"] = (c.rolling(20).max() == c).astype(int)
    return f


_DIRECTION = "long"


def outcome(df, i):
    """Forward net R for a position entered at the next bar's open."""
    atr = df["_atr"].iloc[i]
    if not np.isfinite(atr) or atr <= 0 or i + 1 >= len(df):
        return None
    e = float(df["o"].iloc[i + 1])
    if e <= 0:
        return None
    risk = ATR_STOP_MULT * atr
    long_ = _DIRECTION == "long"
    stop = e - risk if long_ else e + risk
    target = e + RR_TARGET * risk if long_ else e - RR_TARGET * risk
    hi = df["h"].values
    lo = df["l"].values
    cl = df["c"].values
    end = min(i + 1 + HOLD_BARS, len(df))
    g = None
    for j in range(i + 1, end):
        if long_:
            if lo[j] <= stop:
                g = (stop - e) / e
                break
            if hi[j] >= target:
                g = (target - e) / e
                break
        else:
            if hi[j] >= stop:
                g = (e - stop) / e
                break
            if lo[j] <= target:
                g = (e - target) / e
                break
    if g is None:
        if end - 1 <= i:
            return None
        g = ((cl[end - 1] - e) / e) if long_ else ((e - cl[end - 1]) / e)
    return (g - COST_BPS / 10000.0) / (risk / e)


def build_table(universe, period):
    rows = []
    for sym in universe:
        df = load(sym, period)
        if df is None or len(df) < 300:
            continue
        f = features(df)
        df = df.copy()
        df["_atr"] = f["atr"]
        outs = [outcome(df, i) for i in range(len(df))]
        f["net_r"] = outs
        f["ts"] = df["ts"]
        f["symbol"] = sym
        need = ["net_r", "ext", "rsi", "slope", "volrank", "ret5",
                "volratio", "dd52", "gap", "clpos", "rangeratio", "ext50",
                "ext200", "volchg", "pos52", "voltrend", "ret20"]
        rows.append(f.dropna(subset=[c for c in need if c in f.columns]))
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def add_relative_strength(tab):
    """Relative strength vs the market, computed across symbols per date."""
    if "ret20" not in tab.columns:
        return tab
    tab = tab.copy()
    tab["rs_rank"] = tab.groupby("ts")["ret20"].rank(pct=True)
    return tab


def atomic_conditions(tab):
    """Bin every feature into interpretable conditions. Bins come from
    TRAIN quantiles only, so the holdout cannot leak through binning."""
    conds = {}

    def q(col, lo, hi, name):
        a, b = tab[col].quantile([lo, hi])
        conds[name] = (col, float(a), float(b))

    q("ext", 0.0, 0.2, "ext=very_low")
    q("ext", 0.2, 0.4, "ext=low")
    q("ext", 0.6, 0.8, "ext=high")
    q("ext", 0.8, 1.0, "ext=very_high")
    q("rsi", 0.0, 0.2, "rsi=oversold")
    q("rsi", 0.4, 0.6, "rsi=mid")
    q("rsi", 0.8, 1.0, "rsi=overbought")
    q("slope", 0.0, 0.25, "slope=falling")
    q("slope", 0.75, 1.0, "slope=rising")
    q("volrank", 0.0, 0.3, "vol=low")
    q("volrank", 0.7, 1.0, "vol=high")
    q("ret5", 0.0, 0.25, "ret5=weak")
    q("ret5", 0.75, 1.0, "ret5=strong")
    q("ret1", 0.0, 0.25, "ret1=down")
    q("ret1", 0.75, 1.0, "ret1=up")
    q("volratio", 0.75, 1.0, "volume=surge")
    q("volratio", 0.0, 0.25, "volume=quiet")
    q("dd52", 0.0, 0.25, "dd52=deep")
    q("dd52", 0.75, 1.0, "dd52=near_high")
    # kitchen sink conditions
    q("gap", 0.0, 0.2, "gap=down")
    q("gap", 0.8, 1.0, "gap=up")
    q("clpos", 0.0, 0.25, "close=at_low")
    q("clpos", 0.75, 1.0, "close=at_high")
    q("rangeratio", 0.0, 0.25, "range=narrow")
    q("rangeratio", 0.75, 1.0, "range=wide")
    q("ext50", 0.0, 0.2, "ext50=below")
    q("ext50", 0.8, 1.0, "ext50=above")
    q("ext200", 0.0, 0.2, "ext200=below")
    q("ext200", 0.8, 1.0, "ext200=above")
    q("volchg", 0.0, 0.25, "vol=contracting")
    q("volchg", 0.75, 1.0, "vol=expanding")
    q("pos52", 0.0, 0.2, "pos52=bottom")
    q("pos52", 0.8, 1.0, "pos52=top")
    q("voltrend", 0.0, 0.25, "voltrend=fading")
    q("voltrend", 0.75, 1.0, "voltrend=building")
    q("ret20", 0.0, 0.2, "ret20=weak")
    q("ret20", 0.8, 1.0, "ret20=strong")
    if "rs_rank" in tab.columns:
        q("rs_rank", 0.0, 0.2, "rs=laggard")
        q("rs_rank", 0.8, 1.0, "rs=leader")
    return conds


def mask_for(tab, conds, name):
    col, a, b = conds[name]
    return (tab[col] >= a) & (tab[col] <= b)


def search(tab, conds, max_depth, min_n):
    """Enumerate slices; return t-stat and mean for each."""
    base = {n: mask_for(tab, conds, n).values for n in conds}
    base["regime=up"] = (tab["regime"] == "up").values
    base["regime=down"] = (tab["regime"] == "down").values
    base["day=mon"] = (tab["dow"] == 0).values
    base["day=fri"] = (tab["dow"] == 4).values
    base["streak=up3"] = (tab["streak_up"] >= 3).values
    base["streak=dn3"] = (tab["streak_dn"] >= 3).values
    base["at_20d_high"] = (tab["since_high"] == 1).values
    y = tab["net_r"].values
    names = list(base)
    results = []
    for depth in range(1, max_depth + 1):
        for combo in itertools.combinations(names, depth):
            m = base[combo[0]]
            for extra in combo[1:]:
                m = m & base[extra]
            n = int(m.sum())
            if n < min_n:
                continue
            vals = y[m]
            sd = vals.std(ddof=1)
            if sd <= 0:
                continue
            mean = vals.mean()
            t = mean / (sd / np.sqrt(n))
            results.append({"slice": " + ".join(combo), "n": n,
                            "mean_r": float(mean), "t": float(t)})
    return results


def bh_fdr(pvals, alpha=0.05):
    """Benjamini-Hochberg. Returns the p-value threshold for significance."""
    p = np.sort(np.asarray(pvals))
    m = len(p)
    if m == 0:
        return 0.0
    thresh = alpha * np.arange(1, m + 1) / m
    passed = p <= thresh
    return float(p[passed].max()) if passed.any() else 0.0


def two_sided_p(t, n):
    from math import erfc, sqrt
    return erfc(abs(t) / sqrt(2.0))  # normal approx, fine at these n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--min-n", type=int, default=150)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--period", default="15y")
    ap.add_argument("--shuffle-control", action="store_true")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--seed", type=int, default=20260814)
    ap.add_argument("--direction", choices=["long", "short"], default="long")
    args = ap.parse_args()

    print("Building feature table...", file=sys.stderr)
    global _DIRECTION
    _DIRECTION = args.direction
    tab = build_table(UNIVERSE, args.period)
    if tab.empty:
        print("no data")
        return 1

    tab = add_relative_strength(tab)
    tab = tab.sort_values("ts").reset_index(drop=True)

    # CRITICAL: measure EXCESS return, not absolute.
    #
    # The first run of this engine reported 28 slices surviving all three
    # stages with t-stats up to 20. It was entirely spurious. The tell:
    # regime=up (+0.11 R) and regime=down (+0.27 R) BOTH "held" -- but they
    # partition the data, and an edge cannot exist in both halves of a
    # partition. Cause: baseline mean net R across all bars was +0.1439,
    # because a long-only 2R-target rule on liquid equities over 15 years
    # inherits the equity risk premium. Every slice looked positive because
    # the POPULATION is positive. The search rediscovered "stocks went up",
    # 1,027 times, and dressed it as 28 discoveries.
    #
    # Demeaning per symbol also removes the "which ticker ran hardest"
    # effect, so a slice can only score by beating that symbol's own
    # average bar -- i.e. by TIMING, which is the only thing a rule can
    # actually contribute.
    tab["raw_r"] = tab["net_r"]
    tab["net_r"] = tab["net_r"] - tab.groupby("symbol")["net_r"].transform("mean")

    n = len(tab)
    i_tr, i_va = int(n * 0.50), int(n * 0.75)
    train = tab.iloc[:i_tr]
    valid = tab.iloc[i_tr:i_va]
    hold = tab.iloc[i_va:]

    print(f"rows {n:,}  symbols {tab['symbol'].nunique()}")
    print(f"  train   {len(train):>7,}  {train['ts'].min().date()} .. {train['ts'].max().date()}")
    print(f"  valid   {len(valid):>7,}  {valid['ts'].min().date()} .. {valid['ts'].max().date()}")
    print(f"  holdout {len(hold):>7,}  {hold['ts'].min().date()} .. {hold['ts'].max().date()}")
    print(f"  raw mean net R (all bars, train)      = {train['raw_r'].mean():+.4f}"
          f"   <- the drift a rule must BEAT, not inherit")
    print(f"  excess mean net R after demeaning     = {train['net_r'].mean():+.4f}"
          f"   <- must be ~0 by construction")
    print()

    conds = atomic_conditions(train)
    res = search(train, conds, args.max_depth, args.min_n)

    # Empirical noise floor: rerun the identical search on shuffled
    # outcomes. Any survivor there is definitionally spurious, so this
    # gives a measured false-positive count to compare against, rather
    # than a theoretical one.
    rng = np.random.default_rng(args.seed)
    sh = train.copy()
    sh["net_r"] = rng.permutation(sh["net_r"].values)
    shuf = search(sh, conds, args.max_depth, args.min_n)
    for r in shuf:
        r["p"] = two_sided_p(r["t"], r["n"])
    shuf_hits = sum(1 for r in shuf if r["p"] < args.alpha)
    if not res:
        print("no slices met the minimum sample")
        return 0

    for r in res:
        r["p"] = two_sided_p(r["t"], r["n"])
    m = len(res)
    raw_hits = sum(1 for r in res if r["p"] < args.alpha)
    expected = m * args.alpha
    cutoff = bh_fdr([r["p"] for r in res], args.alpha)
    fdr_hits = [r for r in res if r["p"] <= cutoff and r["mean_r"] > 0]

    print(f"HYPOTHESES TESTED           {m:,}")
    print(f"  raw p<{args.alpha} hits          {raw_hits:,}")
    print(f"  EXPECTED BY CHANCE        {expected:,.0f}")
    print(f"  ratio observed/expected   {raw_hits / max(expected, 1e-9):.2f}x")
    print(f"  SHUFFLED-LABEL control    {shuf_hits:,}   <- measured noise floor")
    print(f"  BH-FDR threshold p<=      {cutoff:.6f}")
    print(f"  survive FDR (positive)    {len(fdr_hits):,}")
    print()
    if raw_hits <= max(expected, shuf_hits) * 1.5:
        print("  >>> Raw hits are at or near the chance floor. Any 'winner'")
        print("      below is most likely noise. This is the outcome the")
        print("      9,000-backtest video did not check for.")
        print()

    if not fdr_hits:
        print("NOTHING survives FDR correction on train. Stopping here --")
        print("carrying candidates forward anyway would be the exact error")
        print("this engine exists to prevent.")
        return 0

    fdr_hits.sort(key=lambda r: -r["t"])
    print(f"{'slice':<58}{'n':>7}{'meanR':>9}{'t':>7}")
    print("-" * 82)
    for r in fdr_hits[:15]:
        print(f"{r['slice']:<58}{r['n']:>7,}{r['mean_r']:>9.4f}{r['t']:>7.2f}")

    # Validation stage
    print("\n" + "=" * 82)
    print("VALIDATION (unseen window; survivors of train only)")
    print(f"{'slice':<58}{'n':>7}{'meanR':>9}{'t':>7}")
    print("-" * 82)
    survivors = []
    for r in fdr_hits[:30]:
        parts = r["slice"].split(" + ")
        mv = np.ones(len(valid), dtype=bool)
        for p in parts:
            mv &= (valid["regime"] == p.split("=")[1]).values if p.startswith("regime=") \
                else mask_for(valid, conds, p).values
        nv = int(mv.sum())
        if nv < args.min_n // 2:
            continue
        vals = valid["net_r"].values[mv]
        sd = vals.std(ddof=1)
        if sd <= 0:
            continue
        t = vals.mean() / (sd / np.sqrt(nv))
        print(f"{r['slice']:<58}{nv:>7,}{vals.mean():>9.4f}{t:>7.2f}")
        if vals.mean() > 0 and t > 1.96:
            survivors.append(r)

    if not survivors:
        print("\nNo train survivor holds up on validation. Stop.")
        return 0

    # Holdout - touched once
    print("\n" + "=" * 82)
    print("HOLDOUT (touched ONCE, informs nothing)")
    print(f"{'slice':<58}{'n':>7}{'meanR':>9}{'t':>7}")
    print("-" * 82)
    final = []
    for r in survivors:
        parts = r["slice"].split(" + ")
        mh = np.ones(len(hold), dtype=bool)
        for p in parts:
            mh &= (hold["regime"] == p.split("=")[1]).values if p.startswith("regime=") \
                else mask_for(hold, conds, p).values
        nh = int(mh.sum())
        if nh < 30:
            continue
        vals = hold["net_r"].values[mh]
        sd = vals.std(ddof=1)
        if sd <= 0:
            continue
        t = vals.mean() / (sd / np.sqrt(nh))
        tag = "  ** HOLDS **" if (vals.mean() > 0 and t > 1.96) else ""
        print(f"{r['slice']:<58}{nh:>7,}{vals.mean():>9.4f}{t:>7.2f}{tag}")
        final.append({"slice": r["slice"], "n": nh,
                      "mean_r": float(vals.mean()), "t": float(t)})

    kept = [f for f in final if f["t"] > 1.96 and f["mean_r"] > 0]
    print(f"\nSurvived all three stages: {len(kept)}")
    print(f"Bonferroni-adjusted threshold for {m:,} hypotheses: "
          f"p < {args.alpha / m:.2e} (|t| > "
          f"{abs(np.sqrt(2) * 2.3):.1f} roughly)")
    if kept:
        print("Even these need out-of-universe confirmation before use.")

    if args.json:
        args.json.write_text(json.dumps(
            {"hypotheses": m, "raw_hits": raw_hits, "expected": expected,
             "fdr_cutoff": cutoff, "final": final}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
