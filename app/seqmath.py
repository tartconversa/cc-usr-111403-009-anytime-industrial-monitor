"""随时有效序列计算：正态混合 Hoeffding 置信序列与检验鞅。

构造依据 Howard, Ramdas, McAuliffe, Sekhon (2021) 的 sub-Gaussian
正态混合边界：若增量满足 Hoeffding 引理的 sub-Gaussian 性质
（有界随机变量自动满足），则对任意查看次数与停止时间一致有效。
所有证据量在对数空间累计与报告：持续连乘的下溢/上溢不会被
当作证据消失或证据爆炸。
"""
import math

LN10 = math.log(10.0)


def score_bounds(low, high, p):
    """IPW 得分 T*Y/p-(1-T)*Y/(1-p) 在 Y∈[low,high] 时的取值范围 [ℓ, u]。"""
    if not 0 < p < 1:
        raise ValueError('分配概率应在(0,1)内')
    return min(low / p, -high / (1 - p)), max(high / p, -low / (1 - p))


def mixture_radius(variance, rho2, alpha):
    """中心化累计和 S_t 的随时有效半径：P(存在t: |S_t| >= radius) <= alpha。

    variance 为 Hoeffding 平方和 V_t=Σb_i^2（b_i 为各步半宽），
    rho2 为冻结的尺度参数（只影响宽度，不影响有效性）。
    """
    if variance <= 0 or rho2 <= 0 or not 0 < alpha < 1:
        raise ValueError('半径参数无效')
    return math.sqrt(2.0 * (variance + rho2) * (math.log(2.0 / alpha) + 0.5 * math.log1p(variance / rho2)))


def log10_anytime_p(sum_score, variance, rho2):
    """双侧随时有效 p 值的 log10；弱原假设：累计平均效应为 0。

    p_t <= alpha 当且仅当 0 落在 alpha 级置信序列之外，因此
    P(存在t: p_t <= alpha) <= alpha。p>1 时截为 1（log10=0）。
    """
    if variance <= 0:
        return 0.0
    ln_p = (math.log(2.0) + 0.5 * math.log1p(variance / rho2)
            - sum_score * sum_score / (2.0 * (variance + rho2)))
    return min(0.0, ln_p / LN10)


def log10_test_martingale(sum_score, variance, lam):
    """检验鞅 L_t=exp(λS_t-λ²V_t/2) 的 log10；lam 为冻结的正参数。

    在尖锐原假设（每个企业效应均 <= 0）下 L_t 是非负上鞅，
    Ville 不等式给出 P(存在t: L_t >= 1/alpha) <= alpha。
    """
    if lam <= 0:
        raise ValueError('检验鞅参数应为正')
    return (lam * sum_score - 0.5 * lam * lam * variance) / LN10
