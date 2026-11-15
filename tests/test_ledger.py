"""审计账本：版本链、停止决定不可变、冲突确认、预算与回放。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from app import anytime
from app.ledger import Ledger, LedgerError
from app.spec import SpecError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def ent(eid, order, treated, p, registered=0, attributes=None):
    row = {'id': eid, 'revision': 1, 'order': order, 'treated': treated,
           'probability': p, 'registered': registered}
    if attributes is not None:
        row['attributes'] = attributes
    return row


def out(oid, enterprise, value, known, revision=1, deleted=False):
    if deleted:
        return {'id': oid, 'revision': revision, 'deleted': True}
    return {'id': oid, 'revision': revision, 'enterprise': enterprise,
            'value': value, 'known': known}


def payload(assignments, outcomes, as_of=10, p_min=0.2, bounds=(0, 10), version='t'):
    return {'schema_version': 1,
            'metadata': {'as_of': as_of, 'p_min': p_min, 'bounds': list(bounds),
                         'data_version': version},
            'collections': {'assignments': assignments, 'outcomes': outcomes}}


SPEC_RAW = {'spec_id': 't', 'alpha_total': 0.05, 'p_min': 0.2, 'frozen_at': 0,
            'metrics': {'m': {'bounds': [0, 10], 'delta': 0.5}},
            'groups': {'all': {}, 'mid': {'attributes': {'size': 'mid'}}},
            'cells': [{'metric': 'm', 'group': 'all', 'weight': 0.7},
                      {'metric': 'm', 'group': 'mid', 'weight': 0.3}]}


def strong_payload(n, as_of, treated_y=9.0, control_y=1.0, flip_treated=()):
    """n家企业交替分配，处理组结果高：足够大的前缀会触发benefit停止。"""
    assignments, outcomes = [], []
    for i in range(1, n + 1):
        treated = i % 2 == 0
        y = treated_y if treated else control_y
        if i in flip_treated:
            y = control_y
        assignments.append(ent(f'e{i}', i, treated, 0.5))
        outcomes.append(out(f'o{i}', f'e{i}', y, known=as_of))
    return payload(assignments, outcomes, as_of=as_of, version=f'as{as_of}')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def init_ledger(self, spec=SPEC_RAW):
        return Ledger.init(self.dir, spec)

    def test_init_freezes_spec_and_rejects_different_spec(self):
        ledger = self.init_ledger()
        self.assertEqual(len(ledger.spec.digest), 64)
        Ledger.init(self.dir, SPEC_RAW)  # 同内容幂等
        with self.assertRaisesRegex(LedgerError, '不能覆盖'):
            Ledger.init(self.dir, dict(SPEC_RAW, spec_id='other'))

    def test_weight_budget_enforced(self):
        bad = dict(SPEC_RAW, cells=[{'metric': 'm', 'group': 'all', 'weight': 0.7},
                                    {'metric': 'm', 'group': 'mid', 'weight': 0.4}])
        with self.assertRaisesRegex(SpecError, '预算'):
            Ledger.init(self.dir, bad)

    def test_repeat_run_is_auditable_version(self):
        self.init_ledger()
        p = strong_payload(4, 10)
        r1 = Ledger(self.dir).record_run(p)
        r2 = Ledger(self.dir).record_run(p)
        self.assertEqual(r1['kind'], 'compute')
        self.assertEqual(r2['kind'], 'repeat')
        self.assertEqual(r2['cells'], r1['cells'])
        self.assertEqual(len(Ledger(self.dir).versions()), 2)

    def test_stop_decision_and_immutability(self):
        self.init_ledger()
        r1 = Ledger(self.dir).record_run(strong_payload(200, 10))
        self.assertEqual([d['kind'] for d in r1['new_decisions']], ['benefit'])
        self.assertEqual(r1['new_decisions'][0]['cell'], 'm|all')
        r2 = Ledger(self.dir).record_run(strong_payload(240, 20))
        self.assertEqual(r2['new_decisions'], [])  # 已停止单元不再重复决定
        log = Ledger(self.dir).decision_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]['status'], 'standing')

    def test_correction_flags_conflict_and_keeps_decision(self):
        self.init_ledger()
        Ledger(self.dir).record_run(strong_payload(200, 10))
        # 更正：前200家里120家处理组结果改低，历史前缀上的停止条件不再成立
        corrected = strong_payload(200, 10, flip_treated=set(range(2, 242, 2)))
        corrected['metadata']['as_of'] = 20
        r = Ledger(self.dir).record_run(corrected)
        self.assertEqual(r['kind'], 'compute')
        self.assertEqual(len(r['conflicts']), 1)
        self.assertEqual(r['conflicts'][0]['reason'], 'CONDITION_NOT_REPRODUCED')
        self.assertFalse(r['conflicts'][0]['acknowledged'])
        log = Ledger(self.dir).decision_log()
        self.assertEqual(log[0]['status'], 'conflicted')
        Ledger(self.dir).acknowledge(log[0]['decision_id'], '张三', '源系统更正，维持原决定')
        self.assertEqual(Ledger(self.dir).decision_log()[0]['status'], 'acknowledged')
        # 原决定记录始终保留
        self.assertEqual(Ledger(self.dir).decision_log()[0]['kind'], 'benefit')

    def test_prefix_changed_conflict(self):
        self.init_ledger()
        Ledger(self.dir).record_run(strong_payload(200, 10))
        p = strong_payload(200, 10)
        p['collections']['outcomes'][0] = out('o1', 'e1', 0, 0, revision=2, deleted=True)
        p['metadata']['as_of'] = 20
        r = Ledger(self.dir).record_run(p)
        self.assertEqual(r['conflicts'][0]['reason'], 'PREFIX_CHANGED')

    def test_conflicting_revision_produces_rejected_version(self):
        self.init_ledger()
        p = strong_payload(4, 10)
        row = dict(p['collections']['outcomes'][0])
        row['value'] = 1.5  # 同id同revision不同内容
        p['collections']['outcomes'].append(row)
        r = Ledger(self.dir).record_run(p)
        self.assertEqual(r['kind'], 'rejected')
        self.assertIn('冲突', r['error'])
        r2 = Ledger(self.dir).record_run(strong_payload(4, 10))
        self.assertEqual(r2['kind'], 'compute')
        self.assertEqual(r2['ledger_version'], 2)

    def test_as_of_regression_rejected(self):
        self.init_ledger()
        Ledger(self.dir).record_run(strong_payload(4, 10))
        r = Ledger(self.dir).record_run(strong_payload(6, 5))
        self.assertEqual(r['kind'], 'rejected')
        self.assertIn('as_of', r['error'])

    def test_amendment_uses_only_remaining_budget_and_is_forward_only(self):
        spec = dict(SPEC_RAW, cells=[{'metric': 'm', 'group': 'all', 'weight': 0.5},
                                     {'metric': 'm', 'group': 'mid', 'weight': 0.3}])
        Ledger.init(self.dir, spec)  # 剩余预算 0.2
        Ledger(self.dir).record_run(strong_payload(4, 10))
        over = {'amendment_id': 'a1', 'effective_from': 11,
                'groups': {'big': {'attributes': {'size': 'big'}}},
                'cells': [{'metric': 'm', 'group': 'big', 'weight': 0.3}]}
        with self.assertRaisesRegex(SpecError, '剩余预算'):
            Ledger(self.dir).amend(over)
        retro = {'amendment_id': 'a2', 'effective_from': 10,
                 'groups': {'big': {'attributes': {'size': 'big'}}},
                 'cells': [{'metric': 'm', 'group': 'big', 'weight': 0.2}]}
        with self.assertRaisesRegex(SpecError, '不能回溯'):
            Ledger(self.dir).amend(retro)
        ok = dict(retro, amendment_id='a3', effective_from=11)
        result = Ledger(self.dir).amend(ok)
        self.assertAlmostEqual(result['remaining_weight'], 0.0)
        # 修订单元只消费 effective_from 之后到达的结果
        p = payload([ent('e1', 1, True, 0.5, attributes={'size': 'big'})],
                    [out('o1', 'e1', 8, 5)], as_of=20)
        r = Ledger(self.dir).record_run(p)
        cell = next(c for c in r['cells'] if c['cell'] == 'm|big')
        self.assertEqual(cell['blocked']['reason'], 'OUTCOME_PREDATES_FREEZE')
        p2 = payload([ent('e1', 1, True, 0.5, attributes={'size': 'big'})],
                     [out('o1', 'e1', 8, 15)], as_of=20)
        r2 = Ledger(self.dir).record_run(p2)
        cell2 = next(c for c in r2['cells'] if c['cell'] == 'm|big')
        self.assertEqual(cell2['n'], 1)

    def test_exploratory_group_never_decides(self):
        self.init_ledger()
        explore = [{'name': 'lucky', 'attributes': {}}]
        r = Ledger(self.dir).record_run(strong_payload(200, 10), explore)
        ex = next(c for c in r['cells'] if c['exploratory'])
        self.assertFalse(ex['valid'])
        self.assertIsNone(ex['alpha'])
        self.assertNotIn(ex['cell'], [d['cell'] for d in r['new_decisions']])
        self.assertIn('探索性', ex['note'])

    def test_replay_verifies_chain_and_detects_tamper(self):
        self.init_ledger()
        Ledger(self.dir).record_run(strong_payload(4, 10))
        Ledger(self.dir).record_run(strong_payload(200, 20))
        Ledger(self.dir).record_run(strong_payload(200, 20))  # repeat
        report = Ledger(self.dir).replay()
        self.assertTrue(report['verified'])
        self.assertEqual(len(report['details']), 3)
        single = Ledger(self.dir).replay(2)
        self.assertTrue(single['verified'])
        self.assertEqual(single['record']['version'], 2)
        self.assertEqual(len(single['record']['new_decisions']), 1)
        # 篡改版本文件：重算校验必须发现
        vfile = Path(self.dir) / 'versions' / '000002.json'
        rec = json.loads(vfile.read_text(encoding='utf-8'))
        rec['cells'][0]['estimate'] = 123.0
        vfile.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
        tampered = Ledger(self.dir).replay(2)
        self.assertFalse(tampered['verified'])
        self.assertTrue(tampered['details'][1]['problems'])

    def test_decision_id_format_and_replay_of_decision(self):
        self.init_ledger()
        Ledger(self.dir).record_run(strong_payload(200, 10))
        log = Ledger(self.dir).decision_log()
        self.assertEqual(log[0]['decision_id'], 'm|all#v1')
        replayed = Ledger(self.dir).replay(1)
        self.assertEqual(replayed['record']['new_decisions'][0]['kind'], 'benefit')

    def test_cli_end_to_end(self):
        def call(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                anytime.main(list(argv))
            return json.loads(buf.getvalue())

        frozen = call('init', self.dir, str(FIXTURES / 'pilot_spec.json'))
        self.assertTrue(frozen['frozen'])
        r1 = call('run', self.dir, str(FIXTURES / 'pilot_wave1.json'))
        cell = next(c for c in r1['cells'] if c['cell'] == 'all_sales|all')
        self.assertEqual(cell['blocked']['reason'], 'OUTCOME_PENDING')
        self.assertEqual(cell['waiting_behind'], ['E-05'])
        call('run', self.dir, str(FIXTURES / 'pilot_wave2.json'))
        call('run', self.dir, str(FIXTURES / 'pilot_correction.json'))
        self.assertTrue(call('replay', self.dir, '--all')['verified'])
        self.assertEqual(call('decisions', self.dir)['decisions'], [])


if __name__ == '__main__':
    unittest.main()
