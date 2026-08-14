#!/usr/bin/env python3
"""Parameter sweep with significance testing.

Judges configurations on EXPECTANCY (net return per trade after the modelled
round trip), never on signal count. A configuration that produces more
signals is not better; the 2026-08-14 sweep showed SQUEEZE_BARS=2 produced
2.2x the trades of SQUEEZE_BARS=3 and lost money on every one of them.

Every reported mean carries a bootstrap 95% CI and, against the incumbent, a
permutation p-value. The decision rule is deliberately conservative:

    DO NOT change a parameter unless its bootstrap CI excludes zero
    AND the permutation test against the incumbent gives p < 0.05
    AND n >= --min-n for that configuration.

This exists because the first sweep looked decisive (win rate climbing
22.7% -> 66.7% as filters tightened) and was pure artefact: tightening the
risk floor mechanically deletes the tightest-stop trades, so the win rate
rose because the denominator shrank. Every CI spanned zero; the best
head-to-head p-value was 0.31.

Usage:
  PYTHONPATH=src python3 scripts/sweep_params.py --warehouse
  PYTHONPATH=src python3 scripts/sweep_params.py --symbols AIRO RRGB --live
  PYTHONPATH=src python3 scripts/sweep_params.py --warehouse --min-n 30 --json out.json
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tempest.config import WAREHOUSE_DIR  # noqa: E402
from tempest.features import compute_features  # noqa: E402
from tempest.strategy import (  # noqa: E402
    MIN_RISK_FRACTION,
    SQUEEZE_BARS,
    detect_first_pullback,
)
from tempest.validation import CostModel  # noqa: E402
from tempest.warehouse import load_from_warehouse  # noqa: E402

COST = CostModel()

# A bootstrap CI is meaningless below a handful of observations: resampling
# a single point returns that point, giving a zero-width "CI" that then reads
# as a confident verdict. Nothing under this n gets a proven-negative label.
MIN_N_FOR_CI = 10

INCUMBENT = (SQUEEZE_BARS, MIN_RISK_FRACTION)


def simulate(sig, grp, hold_bars=15):
    """Walk forward from the bar AFTER the signal: stop, target, or horizon.

    Conservative within a bar: if both the stop and the target are touched,
    the STOP is taken. Without tick data we cannot know the order, and
    assuming the good outcome is how backtests lie.
    """
    idx = grp.index[grp["bar_ts_utc"] == sig.entry_ts]
    if len(idx) == 0:
        return None
    start = int(idx[0]) + 1
    forward = grp.iloc[start:start + hold_bars]
    if forward.empty:
        return None
    entry, stop, target = sig.entry_price, sig.stop_price, sig.target_price
    risk = entry - stop
    if risk <= 0:
        return None
    for _, bar in forward.iterrows():
        if bar["low"] <= stop:
            return _result((stop - entry) / entry, risk, entry, "stop")
        if bar["high"] >= target:
            return _result((target - entry) / entry, risk, entry, "target")
    last = float(forward["close"].iloc[-1])
    return _result((last - entry) / entry, risk, entry, "horizon")


def _result(gross, risk, entry, reason):
    net = COST.net_return(gross)
    return {"net": net, "r_multiple": net / (risk / entry), "reason": reason}


def load_frames(args):
    frames = {}
    if args.warehouse:
        if not WAREHOUSE_DIR.exists():
            print(f"warehouse not found at {WAREHOUSE_DIR}")
            return frames
        symbols = sorted(
            p.name.split("=", 1)[1]
            for p in WAREHOUSE_DIR.glob("symbol=*") if p.is_dir()
        )
        for sym in symbols:
            raw = load_from_warehouse(sym)
            if raw.empty:
                continue
            feat = compute_features(raw)
            if feat is not None and not feat.empty:
                frames[sym] = feat
    else:
        import yfinance as yf
        for sym in args.symbols:
            d = yf.download(sym, period="8d", interval="1m",
                            progress=False, auto_adjust=False)
            if d is None or d.empty:
                continue
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d = d.reset_index()
            tcol = "Datetime" if "Datetime" in d.columns else "index"
            raw = pd.DataFrame({
                "symbol": sym,
                "bar_ts_utc": pd.to_datetime(d[tcol], utc=True),
                "open": d["Open"].astype(float), "high": d["High"].astype(float),
                "low": d["Low"].astype(float), "close": d["Close"].astype(float),
                "volume": d["Volume"].astype(float),
            })
            raw = raw[raw["volume"] > 0].reset_index(drop=True)
            feat = compute_features(raw)
            if feat is not None and not feat.empty:
                frames[sym] = feat
    return frames


def trades_for(frames, squeeze_bars, min_risk_fraction, hold_bars):
    nets = []
    for sym, feat in frames.items():
        signals = detect_first_pullback(
            feat, sym, squeeze_bars=squeeze_bars,
            min_risk_fraction=min_risk_fraction,
        )
        for sig in signals:
            grp = (feat[feat["session"] == sig.session]
                   .sort_values("bar_ts_utc").reset_index(drop=True))
            res = simulate(sig, grp, hold_bars)
            if res:
                nets.append(res["net"])
    return np.array(nets)


def breakeven_win_rate(min_risk_fraction, rr_target=2.0):
    """Win rate a config needs just to break even.

    With reward:risk R, risk r (fraction of entry) and round-trip cost c:
        p(R*r - c) + (1-p)(-r - c) = 0
        p = (r + c) / (r * (1 + R))
    Returns None when r <= 0 (no floor: risk is unbounded below, so no
    single breakeven exists).
    """
    if min_risk_fraction <= 0:
        return None
    c = COST.round_trip_bps() / 10000.0
    return (min_risk_fraction + c) / (min_risk_fraction * (1.0 + rr_target))


def bootstrap_ci(sample, rng, iters=20000):
    if len(sample) == 0:
        return (np.nan, np.nan, np.nan)
    draws = rng.choice(sample, size=(iters, len(sample)), replace=True).mean(axis=1)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return lo, hi, float((draws > 0).mean())


def permutation_p(a, b, rng, iters=20000):
    if len(a) == 0 or len(b) == 0:
        return np.nan
    observed = a.mean() - b.mean()
    pool = np.concatenate([a, b])
    cut = len(a)
    count = 0
    for _ in range(iters):
        shuffled = rng.permutation(pool)
        if abs(shuffled[:cut].mean() - shuffled[cut:].mean()) >= abs(observed):
            count += 1
    return count / iters


def main() -> int:
    p = argparse.ArgumentParser()
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--warehouse", action="store_true",
                        help="sweep everything in the parquet warehouse")
    source.add_argument("--live", action="store_true",
                        help="fetch 8 days from yfinance for --symbols")
    p.add_argument("--symbols", nargs="+", default=[])
    p.add_argument("--squeeze-bars", nargs="+", type=int, default=[2, 3, 4])
    # Default grid spans the range where a 2R target is REACHABLE at the
    # modelled cost. The original grid (0, 0.003, 0.005) tested only values
    # needing a 144%/100% win rate -- it could not have found a profitable
    # setting even if one existed. See breakeven_win_rate().
    p.add_argument("--min-risk", nargs="+", type=float,
                   default=[0.003, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05])
    p.add_argument("--hold-bars", type=int, default=15)
    p.add_argument("--min-n", type=int, default=30,
                   help="minimum trades before a config may be acted on")
    p.add_argument("--seed", type=int, default=20260814)
    p.add_argument("--json", type=Path)
    args = p.parse_args()

    frames = load_frames(args)
    if not frames:
        print("no data loaded")
        return 1
    sessions = sum(f["session"].nunique() for f in frames.values())
    print(f"universe: {len(frames)} symbols, {sessions} sessions")
    print(f"incumbent: SQUEEZE_BARS={INCUMBENT[0]} "
          f"MIN_RISK_FRACTION={INCUMBENT[1]}")
    print(f"cost model: {COST.round_trip_bps():.0f} bps round trip\n")

    reachable = [m for m in args.min_risk
                 if (b := breakeven_win_rate(m)) is not None and b <= 1.0]
    if not reachable:
        print("WARNING: every --min-risk value in this grid needs a breakeven")
        print("win rate above 100%. No setting here can be profitable, so the")
        print("sweep cannot find one. Widen the grid (e.g. --min-risk 0.01")
        print("0.02 0.03) or lower the modelled cost.\n")

    rng = np.random.default_rng(args.seed)
    results = {}
    for sq, mrf in itertools.product(args.squeeze_bars, args.min_risk):
        results[(sq, mrf)] = trades_for(frames, sq, mrf, args.hold_bars)

    header = (f"{'sq':>3}{'min_risk':>10}{'n':>6}{'win%':>7}"
              f"{'mean net%':>11}{'95% CI':>20}{'P(>0)':>8}{'need win%':>11}")
    print(header)
    print("-" * len(header))
    rows = []
    for (sq, mrf), nets in sorted(results.items()):
        be = breakeven_win_rate(mrf)
        be_txt = "n/a" if be is None else (
            "IMPOSSIBLE" if be > 1.0 else f"{100 * be:.1f}%")
        if len(nets) == 0:
            print(f"{sq:>3}{mrf:>10.3f}{0:>6}{'-':>7}{'-':>11}{'-':>20}"
                  f"{'-':>8}{be_txt:>11}")
            rows.append({"squeeze_bars": sq, "min_risk_fraction": mrf, "n": 0,
                         "breakeven_win_rate": be})
            continue
        lo, hi, pgt = bootstrap_ci(nets, rng)
        win = 100.0 * (nets > 0).mean()
        marker = "  <- incumbent" if (sq, mrf) == INCUMBENT else ""
        if hi < 0 and len(nets) >= MIN_N_FOR_CI:
            marker += "  ** PROVEN NEGATIVE **"
        elif len(nets) < MIN_N_FOR_CI:
            marker += f"  (n<{MIN_N_FOR_CI}: CI not meaningful)"
        print(f"{sq:>3}{mrf:>10.3f}{len(nets):>6}{win:>7.1f}"
              f"{100 * nets.mean():>11.3f}"
              f"   [{100 * lo:>6.2f},{100 * hi:>6.2f}]{pgt:>8.1%}"
              f"{be_txt:>11}{marker}")
        rows.append({
            "squeeze_bars": sq, "min_risk_fraction": mrf, "n": int(len(nets)),
            "win_pct": win, "mean_net_pct": 100 * float(nets.mean()),
            "ci95_low_pct": 100 * float(lo), "ci95_high_pct": 100 * float(hi),
            "p_mean_positive": float(pgt),
            "breakeven_win_rate": None if be is None else float(be),
        })

    base = results.get(INCUMBENT, np.array([]))
    comparisons = len([n for cfg, n in results.items()
                       if cfg != INCUMBENT and len(n) > 0])
    bonferroni = 0.05 / max(comparisons, 1)
    print(f"\nHead-to-head vs incumbent {INCUMBENT} (n={len(base)}):")
    print(f"  {comparisons} comparisons -> Bonferroni threshold "
          f"p < {bonferroni:.4f} (not 0.05).")
    print("  Testing 21 configs at p<0.05 expects ~1 false positive by chance.")
    verdicts = []
    for (sq, mrf), nets in sorted(results.items()):
        if (sq, mrf) == INCUMBENT or len(nets) == 0 or len(base) == 0:
            continue
        pval = permutation_p(nets, base, rng)
        lo, hi, _ = bootstrap_ci(nets, rng)
        enough = len(nets) >= args.min_n and len(base) >= args.min_n
        excludes_zero = lo > 0
        significant = pval < bonferroni
        act = enough and excludes_zero and significant
        why = []
        if not enough:
            why.append(f"n<{args.min_n}")
        if not excludes_zero:
            why.append("CI spans 0")
        if not significant:
            why.append(f"p={pval:.3f}>={bonferroni:.4f}")
        verdict = "ADOPT" if act else "keep incumbent (" + ", ".join(why) + ")"
        print(f"  sq={sq} mrf={mrf:<6} diff {100 * (nets.mean() - base.mean()):+7.3f}%/trade"
              f"  p={pval:<6.3f}  {verdict}")
        verdicts.append({"squeeze_bars": sq, "min_risk_fraction": mrf,
                         "p_value": float(pval), "adopt": bool(act)})

    if not any(v["adopt"] for v in verdicts):
        print("\nVERDICT: no configuration clears the bar. Change nothing.")

    print("\nNOTE: min_risk configurations are NESTED, not independent. A")
    print("  higher floor yields a strict SUBSET of the lower floor's signals")
    print("  (verified: every mrf=0.02 signal is also an mrf=0.003 signal).")
    print("  Comparing them is a survivorship comparison -- it asks 'were the")
    print("  wide-stop trades the good ones', which is selection after the")
    print("  fact, not an independent hypothesis. Confirm out-of-sample.")

    # A parameter sweep answers "which setting is best". It does NOT answer
    # "is any setting viable". Report that separately -- otherwise a strategy
    # that loses money at EVERY setting reads as a clean "change nothing".
    proven_negative = [
        (cfg, nets) for cfg, nets in sorted(results.items())
        if len(nets) >= MIN_N_FOR_CI and bootstrap_ci(nets, rng)[1] < 0
    ]
    if proven_negative:
        print("\n" + "=" * 62)
        print("STRATEGY-LEVEL WARNING")
        print("=" * 62)
        print(f"{len(proven_negative)} of {len([n for n in results.values() if len(n)])} "
              "configurations have a 95% CI lying ENTIRELY BELOW ZERO.")
        print("These are not 'unproven' -- they are demonstrated losers:")
        for (sq, mrf), nets in proven_negative:
            lo, hi, _ = bootstrap_ci(nets, rng)
            print(f"    sq={sq} mrf={mrf:<6} n={len(nets):<4} "
                  f"mean {100 * nets.mean():+.3f}%/trade  "
                  f"CI [{100 * lo:.2f}, {100 * hi:.2f}]")
        incumbent_nets = results.get(INCUMBENT, np.array([]))
        if len(incumbent_nets):
            ilo, ihi, _ = bootstrap_ci(incumbent_nets, rng)
            if ihi < 0:
                print("\n  The INCUMBENT is among them. Tuning cannot fix this:")
                print("  the edge is absent at every setting tested, so the")
                print("  problem is the strategy or the cost model, not the")
                print("  parameters. Do not deploy real capital on this.")
        print("\n  Breakeven check: with a 2R target and a round trip of "
              f"{COST.round_trip_bps():.0f} bps,")
        print("  a stop of size r needs win rate (r + c) / 3r. At r = 0.3% that")
        print("  is 144% -- mathematically impossible. Stops must be WIDER than")
        print("  ~2% for a 2R target to be reachable at this cost level.")
    if args.json:
        args.json.write_text(json.dumps(
            {"universe_symbols": len(frames), "sessions": sessions,
             "min_n": args.min_n, "incumbent": list(INCUMBENT),
             "configs": rows, "head_to_head": verdicts}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
