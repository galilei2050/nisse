"""The tail measure must not quietly be the maximum.

`make cost` is read for its p90 — PRICING.md's whole argument is that a small tail carries most of
the bill. `int(n * share)` lands one rank high, so for any agent whose run count is a multiple of
ten the p90 printed was the single dearest run, and nothing in the output said so.
"""

from scripts.cost_report import _percentile


def test_p90_of_ten_runs_is_the_ninth_not_the_tenth() -> None:
    runs = [float(n) for n in range(1, 11)]

    assert _percentile(runs, 0.9) == 9.0
    assert _percentile(runs, 0.5) == 5.0


def test_a_single_run_is_its_own_every_percentile() -> None:
    assert _percentile([4.2], 0.9) == 4.2
    assert _percentile([4.2], 0.5) == 4.2
