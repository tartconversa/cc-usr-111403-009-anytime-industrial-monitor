"""序列计算：界、半径、随时p值与对数空间证据的数值行为。"""
import math
import unittest

from app.seqmath import (score_bounds, mixture_radius, log10_anytime_p,
                         log10_test_martingale, LN10)


class SeqmathTests(unittest.TestCase):
    def test_score_bounds_uses_actual_probability(self):
        self.assertEqual(score_bounds(0, 10, 0.5), (-20.0, 20.0))
        lo, hi = score_bounds(0, 10, 0.1)
        self.assertAlmostEqual(lo, -10 / 0.9)
        self.assertAlmostEqual(hi, 100.0)
        lo, hi = score_bounds(-5, 5, 0.25)
        self.assertEqual(lo, min(-5 / 0.25, -5 / 0.75))
        self.assertEqual(hi, max(5 / 0.25, 5 / 0.75))

    def test_mixture_radius_hand_computed(self):
        # 2*(V+rho2)*(ln(2/alpha)+0.5*ln(1+V/rho2))，V=100, rho2=25, alpha=0.05
        expected = math.sqrt(2 * 125 * (math.log(40) + 0.5 * math.log(5)))
        self.assertAlmostEqual(mixture_radius(100, 25, 0.05), expected)
        self.assertGreater(mixture_radius(200, 25, 0.05), mixture_radius(100, 25, 0.05))
        self.assertGreater(mixture_radius(100, 25, 0.01), mixture_radius(100, 25, 0.05))

    def test_anytime_p_inverts_boundary(self):
        # 在边界上时随时p值应等于alpha
        v, rho2, alpha = 100.0, 25.0, 0.05
        s = mixture_radius(v, rho2, alpha)
        self.assertAlmostEqual(10 ** log10_anytime_p(s, v, rho2), alpha, places=9)
        self.assertEqual(log10_anytime_p(0.0, v, rho2), 0.0)

    def test_log_space_evidence_survives_underflow_and_overflow(self):
        # 每步对数因子 -0.1：朴素连乘在浮点下溢后归零，对数空间精确保留
        lam, b, steps = 0.001, 25.0, 200_000
        per_step = -0.1
        g = (per_step + 0.5 * lam * lam * b * b) / lam
        factor = math.exp(per_step)
        naive = 1.0
        for _ in range(steps):
            naive *= factor
        # 连乘下溢（或停滞在denormal区）：任何下游比较都把证据当作"消失"
        self.assertLess(naive, 1e-300)
        log10_e = log10_test_martingale(steps * g, steps * b * b, lam)
        self.assertTrue(math.isfinite(log10_e))
        self.assertAlmostEqual(log10_e, steps * per_step / LN10, places=6)
        # 反向：每步 +0.1 时朴素连乘溢出为 inf，对数空间仍是有限值
        factor_pos = math.exp(0.1)
        naive_pos = 1.0
        for _ in range(steps):
            naive_pos *= factor_pos
        self.assertTrue(math.isinf(naive_pos))
        g_pos = (0.1 + 0.5 * lam * lam * b * b) / lam
        log10_pos = log10_test_martingale(steps * g_pos, steps * b * b, lam)
        self.assertTrue(math.isfinite(log10_pos))
        self.assertAlmostEqual(log10_pos, steps * 0.1 / LN10, places=6)

    def test_test_martingale_requires_positive_lambda(self):
        with self.assertRaises(ValueError):
            log10_test_martingale(1.0, 1.0, 0.0)


if __name__ == '__main__':
    unittest.main()
