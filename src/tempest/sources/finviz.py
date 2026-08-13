"""Live screener adapter — DEFERRED (see HANDOVER).

The plan: scrape Finviz's premarket screener (HTML) for gap %, relative
volume, price, float; combine with news RSS. This is the live-detection
plane of the strategy. It is intentionally a stub until the backtest shows
the setup pays — a live screener for a strategy that loses after costs is
just faster losing.
"""


class FinvizScreen:
    def premarket_movers(self) -> list[dict]:
        raise NotImplementedError(
            "Finviz scraper is deferred until the backtest justifies it. "
            "See HANDOVER.md 'Deferred'."
        )
