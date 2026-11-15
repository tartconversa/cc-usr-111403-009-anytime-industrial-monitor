"""随时有效保证的模拟验证：覆盖率、检验鞅水平与检验力。

设计式模拟：潜在结果固定，唯一随机性来自企业级独立分配，
分配概率非等（0.2/0.5/0.8），验证构造在任意查看次数下的有效性。
"""
import math
import random
import unittest

from app.seqmath import (score_bounds, mixture_radius, log10_anytime_p,
                         log10_test_martingale)

LOW, HIGH, PMIN = 0.0, 10.0, 0.2
B_MAX = (score_bounds(LOW, HIGH, PMIN)[1] - score_bounds(LOW, HIGH, PMIN)[0]) / 2
RHO2 = (5.0 * B_MAX) ** 2
LAM = 0.5 / (B_MAX ** 2)


def simulate(alpha=0.05, sims=600, n=150, seed=20261115, effect='mixed'):
    rng = random.Random(seed)
    cs_violations = 0
    e_violations = 0
    stops = 0
    threshold = math.log10(1 / alpha)
    for _ in range(sims):
        y0 = [rng.uniform(0, 8) for _ in range(n)]
        if effect == 'null':
            tau = [0.0] * n
        elif effect == 'big':
            tau = [8.0] * n
            y0 = [1.0] * n
        else:
            tau = [rng.uniform(-1, 2) for _ in range(n)]
        y1 = [min(HIGH, max(LOW, y0[i] + tau[i])) for i in range(n)]
        p = [0.5] * n if effect == 'big' else [rng.choice([0.2, 0.5, 0.8]) for _ in range(n)]
        s_centered = 0.0
        s_raw = 0.0
        var = 0.0
        bad_cs = bad_e = stopped = False
        for i in range(n):
            treated = rng.random() < p[i]
            y = y1[i] if treated else y0[i]
            g = y / p[i] if treated else -y / (1 - p[i])
            lo, hi = score_bounds(LOW, HIGH, p[i])
            b = (hi - lo) / 2
            s_centered += g - (y1[i] - y0[i])
            s_raw += g
            var += b * b
            t = i + 1
            if abs(s_centered) > mixture_radius(var, RHO2, alpha):
                bad_cs = True
            if log10_test_martingale(s_raw, var, LAM) > threshold:
                bad_e = True
            half = mixture_radius(var, RHO2, alpha) / t
            if s_raw / t - half > 0.5:
                stopped = True
        cs_violations += bad_cs
        e_violations += bad_e
        stops += stopped
    return cs_violations / sims, e_violations / sims, stops / sims


class CoverageTests(unittest.TestCase):
    def test_confidence_sequence_covers_running_mean_anytime(self):
        # 任意时刻违反率都不应超过alpha（0.05），留出模拟误差余量
        rate, _, _ = simulate()
        self.assertLessEqual(rate, 0.10)

    def test_test_martingale_controls_level_under_sharp_null(self):
        _, rate, _ = simulate(effect='null')
        self.assertLessEqual(rate, 0.10)

    def test_power_under_large_effect(self):
        _, _, stop_rate = simulate(effect='big')
        self.assertGreaterEqual(stop_rate, 0.8)

    def test_anytime_p_consistent_with_interval(self):
        # 区间恰不含0时，随时p值应等于alpha
        rng = random.Random(7)
        for _ in range(20):
            v = rng.uniform(10, 500)
            s = mixture_radius(v, RHO2, 0.05)
            self.assertAlmostEqual(10 ** log10_anytime_p(s, v, RHO2), 0.05, places=6)


if __name__ == '__main__':
    unittest.main()
