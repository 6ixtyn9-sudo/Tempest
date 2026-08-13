"""Tests for the validation/cost layer."""

from tempest.validation import CostModel, bucket_for, summarize, walk_forward_folds


def test_cost_model_nets_round_trip():
    cm = CostModel(spread_bps=25.0, slippage_bps=25.0)
    assert cm.leg_bps() == 50.0
    assert cm.round_trip_bps() == 100.0
    # 1% gross -> 0.99% net with a 100bps round trip.
    assert abs(cm.net_return(0.01) - 0.01 + 0.01) < 1e-9


def test_summarize_empty():
    assert summarize([]) == {"n": 0}


def test_bucket_for_labels_match_the_value():
    """A 3% gap is 2-5%, a 7x relvol is 5-10x — not the next band up."""
    b = bucket_for(0.03, 7.0, 8.0, 10)
    assert b["gap_band"] == "2-5%"
    assert b["relvol_band"] == "5-10x"
    assert b["price_band"] == "5-10"
    b2 = bucket_for(0.12, 25.0, 3.0, 10)
    assert b2["gap_band"] == "10%+"
    assert b2["relvol_band"] == "20x+"
    assert b2["price_band"] == "2-5"


def test_walk_forward_folds_chronological():
    import pandas as pd
    df = pd.DataFrame({"entry_ts": pd.date_range("2026-01-01", periods=10, freq="h")})
    folds = walk_forward_folds(df, n_folds=3)
    assert len(folds) == 3
    for train, valid in folds:
        assert train["entry_ts"].max() < valid["entry_ts"].min()
