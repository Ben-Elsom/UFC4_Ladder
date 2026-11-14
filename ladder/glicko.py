"""Glicko-2 ratings (Glickman 2012). Each reported fight/set is one rating period."""

from __future__ import annotations

import math

START_RATING = 1500.0
START_RD = 200.0   # lower than standard 350 so early swings stay sane for a small group
START_VOL = 0.06
TAU = 0.5
MIN_RD = 45.0          # keeps ratings moving a bit even for regulars
SCALE = 173.7178
DAYS_PER_PERIOD = 7.0  # uncertainty grows by one period per idle week


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def inflate(rd: float, vol: float, idle_days: float) -> float:
    """More uncertainty after time away."""
    periods = max(0.0, idle_days) / DAYS_PER_PERIOD
    phi = rd / SCALE
    return min(START_RD, SCALE * math.sqrt(phi * phi + periods * vol * vol))


def update(r: float, rd: float, vol: float, results: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    """results: (opponent rating, opponent RD, score 1/0) for each game in the period."""
    mu, phi = (r - START_RATING) / SCALE, rd / SCALE
    v_inv, delta_sum = 0.0, 0.0
    for opp_r, opp_rd, s in results:
        mu_j, phi_j = (opp_r - START_RATING) / SCALE, opp_rd / SCALE
        g = _g(phi_j)
        e = 1.0 / (1.0 + math.exp(-g * (mu - mu_j)))
        v_inv += g * g * e * (1 - e)
        delta_sum += g * (s - e)
    v = 1.0 / v_inv
    delta = v * delta_sum

    a = math.log(vol * vol)

    def f(x: float) -> float:
        ex = math.exp(x)
        return ex * (delta * delta - phi * phi - v - ex) / (2 * (phi * phi + v + ex) ** 2) - (x - a) / (TAU * TAU)

    big_a = a
    if delta * delta > phi * phi + v:
        big_b = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        big_b = a - k * TAU
    fa, fb = f(big_a), f(big_b)
    while abs(big_b - big_a) > 1e-6:
        c = big_a + (big_a - big_b) * fa / (fb - fa)
        fc = f(c)
        if fc * fb <= 0:
            big_a, fa = big_b, fb
        else:
            fa /= 2
        big_b, fb = c, fc
    new_vol = math.exp(big_a / 2)

    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * delta_sum
    return START_RATING + SCALE * new_mu, max(MIN_RD, min(START_RD, SCALE * new_phi)), new_vol
