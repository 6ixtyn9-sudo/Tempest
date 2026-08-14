# Autonomous slice discovery: 22,798 hypotheses, nothing survived

`scripts/discover.py` lets the data propose the rules instead of a human
choosing one. It computes descriptive state features, enumerates every
combination up to depth 3, and asks which conditions precede better-than-
baseline forward returns.

## Result

| direction | hypotheses | raw p<0.05 | expected by chance | shuffle control | survived all 3 stages |
|---|---|---|---|---|---|
| long | 11,399 | 4,854 | 570 | 524 | **0** |
| short | 11,399 | 4,305 | 570 | 927 | **0** |

Both directions: 2,151 and 841 slices cleared FDR correction on training
data, and **every one collapsed on validation** — most flipping sign, not
merely fading.

```
vol=high                    train +0.062 (t=8.1)  ->  valid -0.070 (t=-7.0)
rsi=overbought + vol=high   train +0.155 (t=8.1)  ->  valid -0.069 (t=-2.4)
ret5=weak + vol=expanding   train +0.153 (t=8.7)  ->  valid -0.063 (t=-2.4)
```

Sign inversion out of sample is the signature of fitting noise.

## The bug that made run 1 lie, and why it matters

The first version reported **28 slices surviving all three stages** with
t-statistics up to 20. It was completely spurious. The tell was in its own
output:

```
regime=up     n=26,731   +0.1106 R   t=15.2   ** HOLDS **
regime=down   n= 8,132   +0.2684 R   t=20.1   ** HOLDS **
```

Those two conditions partition the data. An edge cannot exist in both
halves of a partition. Same for `ret5=weak`/`ret5=strong` and
`vol=high`/`volume=quiet` — mutually exclusive, all "confirmed".

Cause: baseline mean net R across all bars was **+0.1439**. A long-only
2R-target rule on liquid equities over 15 years inherits the equity risk
premium. Every slice looked profitable because the *population* was
profitable. The engine rediscovered "stocks went up" 1,027 times and
presented it as 28 discoveries.

Fix: outcomes are now demeaned per symbol, so a slice scores only by
beating that symbol's own average bar — i.e. by timing, the one thing a
rule can actually contribute. Note the short run shows raw mean **−0.1486**:
the mirror image, and the same trap in reverse.

## Guards

1. **Three-way time split.** Discovery on train; validation filters; the
   holdout is read once and informs nothing.
2. **Benjamini-Hochberg FDR** across every hypothesis.
3. **Expected-by-chance printed** beside observed hits.
4. **Shuffled-label control** — the identical search on randomised
   outcomes, giving a measured false-positive floor (524 long, 927 short)
   rather than a theoretical one.
5. **Minimum sample** per slice; **R-multiples** not percentages, since
   percent scales with volatility while R is what compounds under
   risk-parity sizing.
6. **Per-symbol demeaning**, above.

## Why this matters beyond Tempest

This is the failure mode of large backtest sweeps generally. A widely
shared video reports 9,000 backtests narrowed to 524 survivors — but
9,000 × 0.05 ≈ 450 false positives are expected from chance alone, and no
correction is mentioned. Its survivor count is approximately its noise
floor. Our own run produced 4,854 raw hits against a 570 chance
expectation and a 524 measured floor; correction plus out-of-sample
testing reduced that to zero.

## Standing evidence

| test | n | outcome |
|---|---|---|
| Gale ORB5, both directions | 62 | zero gross edge |
| First-pullback, 12 market/timeframe cells | 1,097 | 0 positive |
| Overnight gap-hold | 74 | underpowered, inconclusive |
| Autonomous discovery, long + short | 22,798 | 0 survivors |
| **Trend-pullback, daily liquid large-cap** | **1,307** | **+0.083 R, CI [+0.019, +0.153] — PASSED** |

One survivor from the whole programme: buy a large-cap in an established
uptrend when it dips to its 20-day average, 1.5×ATR stop, 2R target. Three
independent confirmations (10 → 12 → 24 fresh symbols) with the effect
attenuating +0.224 → +0.128 → +0.083 R. Plan around the last figure.

That it was *not* rediscovered here is consistent rather than contradictory:
this grid tests state conditions present on any given bar, not entry-timing
rules with sequential structure.

## Usage

```
PYTHONPATH=src python3 scripts/discover.py --direction long
PYTHONPATH=src python3 scripts/discover.py --direction short --max-depth 3
PYTHONPATH=src python3 scripts/discover.py --min-n 300 --json out.json
```

Add features to `features()` and bins to `atomic_conditions()`; the search,
correction and staging apply automatically.
