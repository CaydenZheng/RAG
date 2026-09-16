"""Shared percentile calculations for evaluation and assessment reports."""

import math
from collections.abc import Sequence


def linear_percentile(values: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    if not values:
        raise ValueError("percentile samples cannot be empty")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")

    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction
