"""按冻结顺序消费企业前缀，输出各单元随时有效状态。

消费规则：企业按预登记 order 排序，从首个企业开始连续消费；
首个不完整的企业（结果未到、概率越界、登记未到等）阻塞该单元，
阻塞点之后即使结果已返回也不得提前消费（waiting_behind 仅列出，
不纳入计算）。重复上报内容一致按一条处理并警告，内容冲突则阻塞。
"""
from .seqmath import score_bounds, mixture_radius, log10_anytime_p, log10_test_martingale
from .spec import make_cell, Group


class EngineError(ValueError):
    """资料与冻结方案不一致，或消费顺序被违反。"""


ESTIMAND = '截顶尺度上逐企业处理效应的累计平均：前缀内 mean_i(E[cap(Y_i(1))]-E[cap(Y_i(0))])'


def _is_num(x):
    return type(x) in (int, float)


def _matches(assignment, group):
    attrs = assignment.get('attributes') or {}
    if not isinstance(attrs, dict):
        return False
    return all(attrs.get(k) == v for k, v in group.attributes)


def _check_row(a, metric_name, outcomes, as_of, p_min, effective_from):
    """返回 None 表示该企业可消费，否则返回阻塞原因。"""
    if a['registered'] > as_of:
        return 'ENTERPRISE_NOT_REGISTERED'
    p = a['probability']
    if not _is_num(p) or not (p_min <= p <= 1 - p_min):
        return 'PROPENSITY_OUT_OF_BOUNDS'
    entry = outcomes.get((a['id'], metric_name))
    if entry is None:
        return 'OUTCOME_PENDING'
    status, row = entry
    if status == 'conflict':
        return 'CONFLICTING_OUTCOME_REPORTS'
    if row['known'] > as_of:
        return 'OUTCOME_PENDING'
    if row['known'] < a['registered']:
        return 'OUTCOME_BEFORE_REGISTRATION'
    if row['known'] < effective_from:
        return 'OUTCOME_PREDATES_FREEZE'
    return None


def _assemble(cell, metric, included, sum_g, var, capped, blocked, waiting, explore_alpha):
    n = len(included)
    if n > 0:
        est = sum_g / n
        level = cell.alpha if cell.alpha is not None else explore_alpha
        half = mixture_radius(var, cell.rho2, level) / n
        lower, upper = est - half, est + half
        p10 = log10_anytime_p(sum_g, var, cell.rho2)
        e10 = log10_test_martingale(sum_g, var, cell.lam)
    else:
        est = half = lower = upper = None
        p10 = 0.0
        e10 = 0.0
    state = {
        'cell': cell.key, 'metric': cell.metric, 'group': cell.group,
        'weight': cell.weight, 'alpha': cell.alpha, 'exploratory': cell.exploratory,
        'valid': not cell.exploratory,
        'effective_from': cell.effective_from, 'delta': metric.delta,
        'rho2': cell.rho2, 'test_lambda': cell.lam,
        'n': n, 'prefix': included, 'estimate': est, 'half_width': half,
        'lower': lower, 'upper': upper,
        'log10_anytime_p': p10, 'log10_test_martingale': e10,
        'capped': capped, 'blocked': blocked, 'waiting_behind': waiting,
        'stopping': {'benefit_if_lower_above': metric.delta,
                     'harm_if_upper_below': -metric.delta},
        'estimand': ESTIMAND,
    }
    if cell.exploratory:
        state['note'] = '探索性单元：未分配错误概率预算，区间无随时有效保证，不能触发停止'
    return state


def _cell_state(cell, metric, members, outcomes, as_of, p_min, truncate_n, explore_alpha):
    included = []
    blocked = None
    sum_g = 0.0
    var = 0.0
    capped = 0
    stop_idx = None
    for idx, a in enumerate(members):
        if truncate_n is not None and len(included) >= truncate_n:
            break
        reason = _check_row(a, cell.metric, outcomes, as_of, p_min, cell.effective_from)
        if reason is not None:
            blocked = {'enterprise': a['id'], 'reason': reason}
            stop_idx = idx
            break
        row = outcomes[(a['id'], cell.metric)][1]
        y = min(metric.high, max(metric.low, row['value']))
        if y != row['value']:
            capped += 1
        p = a['probability']
        g = y / p if a['treated'] else -y / (1 - p)
        lo, hi = score_bounds(metric.low, metric.high, p)
        b = (hi - lo) / 2.0
        included.append(a['id'])
        sum_g += g
        var += b * b
    waiting = []
    if blocked is not None:
        for a in members[stop_idx + 1:]:
            if _check_row(a, cell.metric, outcomes, as_of, p_min, cell.effective_from) is None:
                waiting.append(a['id'])
    return _assemble(cell, metric, included, sum_g, var, capped, blocked, waiting, explore_alpha)


def _explore_state(ex, metrics, assignments, outcomes, as_of, spec, warnings):
    if not isinstance(ex, dict):
        warnings.append('探索性分组应为对象，已忽略')
        return None
    name = ex.get('name')
    mname = ex.get('metric') or (next(iter(metrics)) if len(metrics) == 1 else None)
    attrs = ex.get('attributes') or {}
    if not isinstance(name, str) or not name or mname not in metrics or not isinstance(attrs, dict):
        warnings.append(f'探索性分组{name}无效或指标未知，已忽略')
        return None
    metric = metrics[mname]
    group = Group(f'~{name}', tuple(sorted(attrs.items())))
    cell = make_cell(metric, group.name, 0.0, spec.alpha_total, spec.p_min, 0, exploratory=True)
    members = [a for a in assignments if _matches(a, group)]
    return _cell_state(cell, metric, members, outcomes, as_of, spec.p_min, None, spec.alpha_total)


def compute(snapshot, spec, amendments=(), explore=(), truncate=None):
    """对快照计算全部单元状态；truncate={单元键: n} 用于按历史前缀长度重算。"""
    meta = snapshot.metadata
    as_of = meta.get('as_of')
    if type(as_of) is not int:
        raise EngineError('metadata缺少整数as_of')
    p_min = meta.get('p_min')
    if not _is_num(p_min) or p_min != spec.p_min:
        raise EngineError('资料p_min与冻结方案不一致')
    metrics = {m.name: m for m in spec.metrics}
    groups = {g.name: g for g in spec.groups}
    cells = list(spec.cells)
    for am in amendments:
        metrics.update((m.name, m) for m in am.metrics)
        groups.update((g.name, g) for g in am.groups)
        cells.extend(am.cells)
    if len(metrics) == 1 and 'bounds' in meta:
        only = next(iter(metrics.values()))
        if list(meta['bounds']) != [only.low, only.high]:
            raise EngineError('资料bounds与冻结方案的截顶范围不一致')
    assignments = snapshot.rows('assignments')
    orders = [a.get('order') for a in assignments]
    if len(set(orders)) != len(orders):
        raise EngineError('企业顺序重复')
    assignments = sorted(assignments, key=lambda a: a['order'])
    warnings = []
    default_metric = next(iter(metrics)) if len(metrics) == 1 else None
    outcome_rows = {}
    for row in snapshot.rows('outcomes'):
        mname = row.get('metric', default_metric)
        if mname is None:
            warnings.append(f"结果记录{row.get('id')}缺少指标字段，未纳入任何单元")
            continue
        if mname not in metrics:
            warnings.append(f"结果记录{row.get('id')}的指标{mname}未知，已忽略")
            continue
        outcome_rows.setdefault((row.get('enterprise'), mname), []).append(row)
    outcomes = {}
    for key, rows in outcome_rows.items():
        values = {r['value'] for r in rows}
        if len(values) > 1:
            outcomes[key] = ('conflict', None)
        else:
            if len(rows) > 1:
                warnings.append(f'企业{key[0]}指标{key[1]}有{len(rows)}条内容一致的重复上报，按最早known取一条')
            outcomes[key] = ('ok', min(rows, key=lambda r: r['known']))
    states = []
    for cell in cells:
        group = groups[cell.group]
        metric = metrics[cell.metric]
        members = [a for a in assignments if _matches(a, group)]
        tn = (truncate or {}).get(cell.key)
        states.append(_cell_state(cell, metric, members, outcomes, as_of,
                                  spec.p_min, tn, spec.alpha_total))
    for ex in explore or ():
        state = _explore_state(ex, metrics, assignments, outcomes, as_of, spec, warnings)
        if state is not None:
            states.append(state)
    return {'as_of': as_of, 'cells': states, 'warnings': warnings}
