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
    p.add_argument("--min-risk", nargs="+", type=float, default=[0.0, 0.003, 0.005])
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

    rng = np.random.default_rng(args.seed)
    results = {}
    for sq, mrf in itertools.product(args.squeeze_bars, args.min_risk):
        results[(sq, mrf)] = trades_for(frames, sq, mrf, args.hold_bars)

    header = (f"{'sq':>3}{'min_risk':>10}{'n':>6}{'win%':>7}"
              f"{'mean net%':>11}{'95% CI':>20}{'P(>0)':>8}")
    print(header)
    print("-" * len(header))
    rows = []
    for (sq, mrf), nets in sorted(results.items()):
        if len(nets) == 0:
            print(f"{sq:>3}{mrf:>10.3f}{0:>6}{'-':>7}{'-':>11}{'-':>20}{'-':>8}")
            rows.append({"squeeze_bars": sq, "min_risk_fraction": mrf, "n": 0})
            continue
        lo, hi, pgt = bootstrap_ci(nets, rng)
        win = 100.0 * (nets > 0).mean()
        marker = "  <- incumbent" if (sq, mrf) == INCUMBENT else ""
        if hi < 0:
            marker += "  ** PROVEN NEGATIVE **"
        print(f"{sq:>3}{mrf:>10.3f}{len(nets):>6}{win:>7.1f}"
              f"{100 * nets.mean():>11.3f}"
              f"   [{100 * lo:>6.2f},{100 * hi:>6.2f}]{pgt:>8.1%}{marker}")
        rows.append({
            "squeeze_bars": sq, "min_risk_fraction": mrf, "n": int(len(nets)),
            "win_pct": win, "mean_net_pct": 100 * float(nets.mean()),
            "ci95_low_pct": 100 * float(lo), "ci95_high_pct": 100 * float(hi),
            "p_mean_positive": float(pgt),
        })

    base = results.get(INCUMBENT, np.array([]))
    print(f"\nHead-to-head vs incumbent {INCUMBENT} (n={len(base)}):")
    verdicts = []
    for (sq, mrf), nets in sorted(results.items()):
        if (sq, mrf) == INCUMBENT or len(nets) == 0 or len(base) == 0:
            continue
        pval = permutation_p(nets, base, rng)
        lo, hi, _ = bootstrap_ci(nets, rng)
        enough = len(nets) >= args.min_n and len(base) >= args.min_n
        excludes_zero = lo > 0
        significant = pval < 0.05
        act = enough and excludes_zero and significant
        why = []
        if not enough:
            why.append(f"n<{args.min_n}")
        if not excludes_zero:
            why.append("CI spans 0")
        if not significant:
            why.append(f"p={pval:.3f}")
        verdict = "ADOPT" if act else "keep incumbent (" + ", ".join(why) + ")"
        print(f"  sq={sq} mrf={mrf:<6} diff {100 * (nets.mean() - base.mean()):+7.3f}%/trade"
              f"  p={pval:<6.3f}  {verdict}")
        verdicts.append({"squeeze_bars": sq, "min_risk_fraction": mrf,
                         "p_value": float(pval), "adopt": bool(act)})

    if not any(v["adopt"] for v in verdicts):
        print("\nVERDICT: no configuration clears the bar. Change nothing.")

    # A parameter sweep answers "which setting is best". It does NOT answer
    # "is any setting viable". Report that separately -- otherwise a strategy
    # that loses money at EVERY setting reads as a clean "change nothing".
    proven_negative = [
        (cfg, nets) for cfg, nets in sorted(results.items())
        if len(nets) > 0 and bootstrap_ci(nets, rng)[1] < 0
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
