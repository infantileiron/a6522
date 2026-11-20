from math import erfc, hypot, sqrt


def sample_ratio(a: int, b: int, target_b: float) -> tuple[float, float] | None:
    expected_b = (a + b) * target_b
    expected_a = a + b - expected_b
    if min(expected_a, expected_b) < 5:
        return None
    chi_squared = (a - expected_a) ** 2 / expected_a + (b - expected_b) ** 2 / expected_b
    return chi_squared, erfc(sqrt(chi_squared / 2))


def two_proportion_test(a: int, n_a: int, b: int, n_b: int) -> tuple[float, float] | None:
    if not n_a or not n_b:
        return None
    total = n_a + n_b
    successes = a + b
    failures = total - successes
    if min(n_a, n_b) * min(successes, failures) < 5 * total:
        return None
    standard_error = sqrt((successes / total) * (failures / total) * (1 / n_a + 1 / n_b))
    z = (b / n_b - a / n_a) / standard_error
    return z, erfc(abs(z) / sqrt(2))


def wilson(count: int, total: int, critical_z: float) -> tuple[float, float]:
    rate = count / total
    adjustment = critical_z ** 2 / total
    center = rate + adjustment / 2
    radius = critical_z * sqrt((rate * (1 - rate) + adjustment / 4) / total)
    lower = 0.0 if count == 0 else (center - radius) / (1 + adjustment)
    upper = 1.0 if count == total else (center + radius) / (1 + adjustment)
    return lower, upper


def difference_interval(a: int, n_a: int, b: int, n_b: int, critical_z: float) -> tuple[float, float] | None:
    if not n_a or not n_b:
        return None
    a_rate, b_rate = a / n_a, b / n_b
    a_lo, a_hi = wilson(a, n_a, critical_z)
    b_lo, b_hi = wilson(b, n_b, critical_z)
    lift = b_rate - a_rate
    return (
        max(-1.0, lift - hypot(b_rate - b_lo, a_hi - a_rate)),
        min(1.0, lift + hypot(b_hi - b_rate, a_rate - a_lo)),
    )
