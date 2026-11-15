"""随时有效引擎：前缀阻塞、非等概率、截顶、修订与冲突语义。"""
import unittest

from app.catalog import load
from app.engine import compute, EngineError
from app.service import SCHEMAS
from app.spec import freeze


def ent(eid, order, treated, p, registered=0, attributes=None):
    row = {'id': eid, 'revision': 1, 'order': order, 'treated': treated,
           'probability': p, 'registered': registered}
    if attributes is not None:
        row['attributes'] = attributes
    return row


def out(oid, enterprise, value, known, revision=1, metric=None, deleted=False):
    if deleted:
        return {'id': oid, 'revision': revision, 'deleted': True}
    row = {'id': oid, 'revision': revision, 'enterprise': enterprise,
           'value': value, 'known': known}
    if metric is not None:
        row['metric'] = metric
    return row


def payload(assignments, outcomes, as_of=10, p_min=0.2, bounds=(0, 10)):
    return {'schema_version': 1,
            'metadata': {'as_of': as_of, 'p_min': p_min, 'bounds': list(bounds),
                         'data_version': 't'},
            'collections': {'assignments': assignments, 'outcomes': outcomes}}


SPEC_RAW = {'spec_id': 't', 'alpha_total': 0.05, 'p_min': 0.2, 'frozen_at': 0,
            'metrics': {'m': {'bounds': [0, 10], 'delta': 0.5}},
            'groups': {'all': {}, 'mid': {'attributes': {'size': 'mid'}}},
            'cells': [{'metric': 'm', 'group': 'all', 'weight': 0.7},
                      {'metric': 'm', 'group': 'mid', 'weight': 0.3}]}


def run(payload_dict, spec_raw=SPEC_RAW, **kw):
    snap = load(payload_dict, SCHEMAS)
    return compute(snap, freeze(spec_raw), **kw)


def cell_of(result, key):
    return next(c for c in result['cells'] if c['cell'] == key)


class EngineTests(unittest.TestCase):
    def test_prefix_blocks_and_lists_waiting(self):
        p = payload(
            [ent('e1', 1, True, 0.5), ent('e2', 2, False, 0.5),
             ent('e3', 3, True, 0.5), ent('e4', 4, False, 0.5)],
            [out('o1', 'e1', 8, 5), out('o3', 'e3', 6, 6), out('o4', 'e4', 2, 6)])
        cell = cell_of(run(p), 'm|all')
        self.assertEqual(cell['prefix'], ['e1'])
        self.assertEqual(cell['blocked'], {'enterprise': 'e2', 'reason': 'OUTCOME_PENDING'})
        # e3/e4 结果已返回但被挡在后面，不能先挑好结果
        self.assertEqual(cell['waiting_behind'], ['e3', 'e4'])

    def test_unequal_probability_ipw(self):
        p = payload([ent('e1', 1, True, 0.25), ent('e2', 2, False, 0.75)],
                    [out('o1', 'e1', 5, 5), out('o2', 'e2', 5, 5)])
        cell = cell_of(run(p), 'm|all')
        self.assertEqual(cell['n'], 2)
        self.assertAlmostEqual(cell['estimate'], (5 / 0.25 - 5 / 0.25) / 2)

    def test_capping_and_capped_count(self):
        p = payload([ent('e1', 1, True, 0.5), ent('e2', 2, True, 0.5)],
                    [out('o1', 'e1', 999, 5), out('o2', 'e2', -3, 5)])
        cell = cell_of(run(p), 'm|all')
        self.assertEqual(cell['capped'], 2)
        self.assertAlmostEqual(cell['estimate'], (10 / 0.5 + 0 / 0.5) / 2)
        self.assertIn('截顶', cell['estimand'])

    def test_propensity_out_of_bounds_blocks(self):
        p = payload([ent('e1', 1, True, 0.05)], [out('o1', 'e1', 5, 5)])
        cell = cell_of(run(p), 'm|all')
        self.assertEqual(cell['n'], 0)
        self.assertEqual(cell['blocked']['reason'], 'PROPENSITY_OUT_OF_BOUNDS')

    def test_group_attributes_filter(self):
        p = payload([ent('e1', 1, True, 0.5, attributes={'size': 'mid'}),
                     ent('e2', 2, False, 0.5, attributes={'size': 'big'})],
                    [out('o1', 'e1', 8, 5), out('o2', 'e2', 2, 5)])
        result = run(p)
        self.assertEqual(cell_of(result, 'm|mid')['prefix'], ['e1'])
        self.assertEqual(cell_of(result, 'm|all')['prefix'], ['e1', 'e2'])

    def test_correction_revision_and_tombstone(self):
        base = [ent('e1', 1, True, 0.5)]
        p1 = payload(base, [out('o1', 'e1', 8, 5), out('o1', 'e1', 2, 5, revision=2)])
        self.assertAlmostEqual(cell_of(run(p1), 'm|all')['estimate'], 2 / 0.5)
        p2 = payload(base, [out('o1', 'e1', 8, 5), out('o1', 'e1', 0, 0, revision=2, deleted=True)])
        cell = cell_of(run(p2), 'm|all')
        self.assertEqual(cell['blocked']['reason'], 'OUTCOME_PENDING')

    def test_duplicate_reports(self):
        rows = [ent('e1', 1, True, 0.5)]
        same = payload(rows, [out('o1', 'e1', 8, 5), out('o2', 'e1', 8, 6)])
        result = run(same)
        cell = cell_of(result, 'm|all')
        self.assertEqual(cell['n'], 1)
        self.assertTrue(any('重复上报' in w for w in result['warnings']))
        conflict = payload(rows, [out('o1', 'e1', 8, 5), out('o2', 'e1', 3, 6)])
        cell = cell_of(run(conflict), 'm|all')
        self.assertEqual(cell['blocked']['reason'], 'CONFLICTING_OUTCOME_REPORTS')

    def test_outcome_before_registration_blocks(self):
        p = payload([ent('e1', 1, True, 0.5, registered=6)], [out('o1', 'e1', 8, 5)])
        cell = cell_of(run(p), 'm|all')
        self.assertEqual(cell['blocked']['reason'], 'OUTCOME_BEFORE_REGISTRATION')

    def test_outcome_predates_freeze_blocks(self):
        spec = dict(SPEC_RAW, frozen_at=5)
        p = payload([ent('e1', 1, True, 0.5)], [out('o1', 'e1', 8, 3)])
        cell = cell_of(run(p, spec), 'm|all')
        self.assertEqual(cell['blocked']['reason'], 'OUTCOME_PREDATES_FREEZE')

    def test_metadata_mismatch_rejected(self):
        rows = [ent('e1', 1, True, 0.5)]
        with self.assertRaisesRegex(EngineError, 'p_min'):
            run(payload(rows, [out('o1', 'e1', 8, 5)], p_min=0.3))
        with self.assertRaisesRegex(EngineError, 'bounds'):
            run(payload(rows, [out('o1', 'e1', 8, 5)], bounds=(0, 99)))

    def test_multi_metric_routing(self):
        spec = {'spec_id': 't2', 'alpha_total': 0.05, 'p_min': 0.2, 'frozen_at': 0,
                'metrics': {'m1': {'bounds': [0, 10], 'delta': 0.5},
                            'm2': {'bounds': [0, 5], 'delta': 0}},
                'groups': {'all': {}},
                'cells': [{'metric': 'm1', 'group': 'all', 'weight': 0.5},
                          {'metric': 'm2', 'group': 'all', 'weight': 0.5}]}
        p = payload([ent('e1', 1, True, 0.5)],
                    [out('o1', 'e1', 8, 5, metric='m1'), out('o2', 'e1', 4, 5)])
        result = run(p, spec)
        self.assertEqual(cell_of(result, 'm1|all')['n'], 1)
        self.assertEqual(cell_of(result, 'm2|all')['blocked']['reason'], 'OUTCOME_PENDING')
        self.assertTrue(any('缺少指标字段' in w for w in result['warnings']))

    def test_zero_prefix_has_unbounded_interval(self):
        p = payload([], [])
        cell = cell_of(run(p), 'm|all')
        self.assertEqual(cell['n'], 0)
        self.assertIsNone(cell['lower'])
        self.assertIsNone(cell['upper'])

    def test_duplicate_order_rejected(self):
        p = payload([ent('e1', 1, True, 0.5), ent('e2', 1, False, 0.5)], [])
        with self.assertRaisesRegex(EngineError, '顺序重复'):
            run(p)


if __name__ == '__main__':
    unittest.main()
