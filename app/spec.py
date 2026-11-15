"""预登记方案的校验、冻结与内容哈希。

方案在结果可知前冻结：指标（截顶范围、最小有意义效应delta）、
分组（按分配记录属性）、单元（指标×分组）及其权重预算。
权重是总错误概率 alpha_total 的 Bonferroni 份额，Σweight<=1；
事后新增分组只能动用剩余预算（amend），不能借用已分配的份额。
"""
import hashlib
import math
from dataclasses import dataclass

from .catalog import canonical
from .seqmath import score_bounds

_WEIGHT_TOL = 1e-9


class SpecError(ValueError):
    """预登记方案或修订违反冻结规则。"""


@dataclass(frozen=True)
class Metric:
    name: str
    low: float
    high: float
    delta: float


@dataclass(frozen=True)
class Group:
    name: str
    attributes: tuple


@dataclass(frozen=True)
class Cell:
    key: str
    metric: str
    group: str
    weight: float
    alpha: object
    rho2: float
    lam: float
    effective_from: int
    exploratory: bool


@dataclass(frozen=True)
class Spec:
    spec_id: str
    alpha_total: float
    p_min: float
    frozen_at: int
    metrics: tuple
    groups: tuple
    cells: tuple
    digest: str


@dataclass(frozen=True)
class Amendment:
    amendment_id: str
    effective_from: int
    metrics: tuple
    groups: tuple
    cells: tuple
    note: str
    digest: str


def content_digest(raw):
    return hashlib.sha256(canonical(raw).encode('utf-8')).hexdigest()


def _num(value):
    return type(value) in (int, float) and math.isfinite(value)


def _name(value, where):
    if not isinstance(value, str) or not value or '|' in value or value.startswith('~'):
        raise SpecError(f'{where}名称应为非空字符串且不含|、不以~开头')
    return value


def _metrics(raw, where, required):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or (required and not raw):
        raise SpecError(f'{where}指标应为对象' + ('且非空' if required else ''))
    out = []
    for name in sorted(raw):
        _name(name, '指标')
        m = raw[name]
        if not isinstance(m, dict):
            raise SpecError(f'指标{name}应为对象')
        bounds = m.get('bounds')
        if not (isinstance(bounds, list) and len(bounds) == 2
                and all(_num(v) for v in bounds) and bounds[0] < bounds[1]):
            raise SpecError(f'指标{name}的截顶范围bounds应为[下界,上界]且下界小于上界')
        delta = m.get('delta', 0)
        if not _num(delta) or delta < 0:
            raise SpecError(f'指标{name}的delta应为非负有限数')
        out.append(Metric(name, float(bounds[0]), float(bounds[1]), float(delta)))
    return tuple(out)


def _groups(raw, where):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SpecError(f'{where}分组应为对象')
    out = []
    for name in sorted(raw):
        _name(name, '分组')
        g = raw[name]
        if g is None:
            g = {}
        if not isinstance(g, dict):
            raise SpecError(f'分组{name}应为对象')
        attrs = g.get('attributes', {})
        if not isinstance(attrs, dict):
            raise SpecError(f'分组{name}的attributes应为对象')
        for k, v in attrs.items():
            if not isinstance(k, str) or not k:
                raise SpecError(f'分组{name}的属性名应为非空字符串')
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise SpecError(f'分组{name}的属性值应为字符串或数值')
        out.append(Group(name, tuple(sorted(attrs.items()))))
    return tuple(out)


def make_cell(metric, group_name, weight, alpha_total, p_min, effective_from,
              exploratory=False, rho2=None, lam=None):
    """构造单元：正态混合尺度 rho2 与检验鞅参数 lam 在此一并冻结。"""
    lo, hi = score_bounds(metric.low, metric.high, p_min)
    b_max = (hi - lo) / 2.0
    if rho2 is None:
        rho2 = (5.0 * b_max) ** 2
    if lam is None:
        target = metric.delta if metric.delta > 0 else (metric.high - metric.low) / 20.0
        lam = target / (b_max ** 2)
    if not (_num(rho2) and rho2 > 0):
        raise SpecError('单元的rho2应为正有限数')
    if not (_num(lam) and lam > 0):
        raise SpecError('单元的lambda应为正有限数')
    alpha = None if exploratory else alpha_total * weight
    return Cell(f'{metric.name}|{group_name}', metric.name, group_name, weight,
                alpha, float(rho2), float(lam), effective_from, exploratory)


def _cells(raw, metrics_by_name, groups_by_name, taken_keys, alpha_total, p_min,
           effective_from, where):
    if not isinstance(raw, list) or not raw:
        raise SpecError(f'{where}单元应为非空数组')
    cells = []
    total = 0.0
    for c in raw:
        if not isinstance(c, dict):
            raise SpecError(f'{where}单元应为对象')
        mname, gname = c.get('metric'), c.get('group')
        if mname not in metrics_by_name:
            raise SpecError(f'{where}单元引用了未知指标{mname}')
        if gname not in groups_by_name:
            raise SpecError(f'{where}单元引用了未知分组{gname}')
        weight = c.get('weight')
        if not _num(weight) or weight <= 0:
            raise SpecError(f'{where}单元权重应为正有限数')
        cell = make_cell(metrics_by_name[mname], gname, float(weight), alpha_total,
                         p_min, effective_from, False, c.get('rho2'), c.get('lambda'))
        if cell.key in taken_keys:
            raise SpecError(f'{where}单元{cell.key}重复')
        taken_keys.add(cell.key)
        cells.append(cell)
        total += cell.weight
    return tuple(cells), total


def freeze(raw):
    """校验并冻结基础方案，返回Spec。"""
    if not isinstance(raw, dict):
        raise SpecError('方案应为对象')
    spec_id = raw.get('spec_id')
    if not isinstance(spec_id, str) or not spec_id:
        raise SpecError('spec_id应为非空字符串')
    alpha_total = raw.get('alpha_total')
    if not _num(alpha_total) or not 0 < alpha_total < 1:
        raise SpecError('alpha_total应在(0,1)内')
    p_min = raw.get('p_min')
    if not _num(p_min) or not 0 < p_min <= 0.5:
        raise SpecError('p_min应在(0,0.5]内')
    frozen_at = raw.get('frozen_at', 0)
    if type(frozen_at) is not int or frozen_at < 0:
        raise SpecError('frozen_at应为非负整数')
    metrics = _metrics(raw.get('metrics'), '方案', True)
    groups = _groups(raw.get('groups'), '方案')
    mbyn = {m.name: m for m in metrics}
    gbyn = {g.name: g for g in groups}
    cells, total = _cells(raw.get('cells'), mbyn, gbyn, set(),
                          float(alpha_total), float(p_min), frozen_at, '方案')
    if total > 1 + _WEIGHT_TOL:
        raise SpecError('单元权重预算之和超过1，不能预支未分配的错误概率')
    return Spec(spec_id, float(alpha_total), float(p_min), frozen_at,
                metrics, groups, cells, content_digest(raw))


def freeze_amendment(raw, known_metrics, known_groups, known_keys, remaining_weight,
                     alpha_total, p_min, min_effective_from):
    """校验并冻结修订：只能动用剩余预算，且只对effective_from之后到达的结果生效。"""
    if not isinstance(raw, dict):
        raise SpecError('修订应为对象')
    amendment_id = raw.get('amendment_id')
    if not isinstance(amendment_id, str) or not amendment_id:
        raise SpecError('amendment_id应为非空字符串')
    effective_from = raw.get('effective_from')
    if type(effective_from) is not int or effective_from < 0:
        raise SpecError('effective_from应为非负整数')
    if min_effective_from is not None and effective_from < min_effective_from:
        raise SpecError('修订生效时点必须晚于已查看数据的as_of，不能回溯')
    metrics = _metrics(raw.get('metrics'), '修订', False)
    groups = _groups(raw.get('groups'), '修订')
    mbyn = {m.name: m for m in known_metrics}
    gbyn = {g.name: g for g in known_groups}
    for m in metrics:
        if m.name in mbyn:
            raise SpecError(f'修订指标{m.name}与已冻结名称重复')
        mbyn[m.name] = m
    for g in groups:
        if g.name in gbyn:
            raise SpecError(f'修订分组{g.name}与已冻结名称重复')
        gbyn[g.name] = g
    cells, total = _cells(raw.get('cells'), mbyn, gbyn, set(known_keys),
                          alpha_total, p_min, effective_from, '修订')
    if total > remaining_weight + _WEIGHT_TOL:
        raise SpecError('修订权重超出剩余预算，事后新增分组不能借用已分配的错误概率')
    note = raw.get('note', '')
    if not isinstance(note, str):
        raise SpecError('note应为字符串')
    return (Amendment(amendment_id, effective_from, metrics, groups, cells,
                      note, content_digest(raw)), total)
