"""审计账本：冻结方案、版本链、不可变停止决定与任意时点回放。

账本目录结构：
  spec.json            冻结的预登记方案及其哈希（init 后不可覆盖）
  amendments.jsonl     追加式修订（只用剩余预算，前向生效）
  versions/NNNNNN.json 版本记录，prev_digest 构成哈希链
  objects/<digest>.json 内容寻址的输入快照（供回放重算）
  decisions.jsonl      决定与确认事件，只追加不修改

每次运行产生一个版本：compute（新输入）、repeat（相同输入的重复上报，
同样入链可审计）、rejected（输入无效，原因入链）。停止决定一经写入
不修改；更正资料后若历史决定在当时前缀上不再成立，新版本报告冲突，
须显式 acknowledge，原决定记录始终保留。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import catalog
from .catalog import canonical
from .engine import compute as compute_cells, EngineError
from .spec import freeze, freeze_amendment, content_digest

SCHEMAS = {'assignments': ['order', 'treated', 'probability', 'registered'],
           'outcomes': ['enterprise', 'value', 'known']}


class LedgerError(ValueError):
    """账本操作违反审计规则。"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, allow_nan=False,
                                     indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _stop_kind(state):
    """冻结的停止规则：区间整体高于delta判benefit，整体低于-delta判harm。"""
    if state['n'] == 0 or state['lower'] is None:
        return None
    if state['lower'] > state['delta']:
        return 'benefit'
    if state['upper'] < -state['delta']:
        return 'harm'
    return None


def _decision_event(cs, version, as_of, spec_digest, input_digest, recorded_at, kind):
    return {'type': 'decision', 'decision_id': f"{cs['cell']}#v{version}",
            'cell': cs['cell'], 'metric': cs['metric'], 'group': cs['group'],
            'kind': kind, 'version': version, 'as_of': as_of, 'n': cs['n'],
            'prefix': cs['prefix'], 'estimate': cs['estimate'],
            'lower': cs['lower'], 'upper': cs['upper'], 'alpha': cs['alpha'],
            'log10_anytime_p': cs['log10_anytime_p'],
            'spec_digest': spec_digest, 'input_digest': input_digest,
            'recorded_at': recorded_at}


class Ledger:
    """一个账本目录绑定一份冻结方案及其全部修订。"""

    def __init__(self, path):
        self.path = Path(path)
        spec_file = self.path / 'spec.json'
        if not spec_file.exists():
            raise LedgerError('账本未初始化，请先init冻结方案')
        stored = _read_json(spec_file)
        self.spec = freeze(stored['spec'])
        if self.spec.digest != stored.get('digest'):
            raise LedgerError('冻结方案哈希不匹配')
        self.amendments = []
        remaining = 1.0 - sum(c.weight for c in self.spec.cells)
        known_m = list(self.spec.metrics)
        known_g = list(self.spec.groups)
        known_keys = {c.key for c in self.spec.cells}
        amend_file = self.path / 'amendments.jsonl'
        if amend_file.exists():
            for line in amend_file.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                am, used = freeze_amendment(entry['amendment'], known_m, known_g,
                                            known_keys, remaining,
                                            self.spec.alpha_total, self.spec.p_min, None)
                if am.digest != entry.get('digest'):
                    raise LedgerError('修订哈希不匹配')
                self.amendments.append(am)
                remaining -= used
                known_m = known_m + list(am.metrics)
                known_g = known_g + list(am.groups)
                known_keys |= {c.key for c in am.cells}

    @classmethod
    def init(cls, path, spec_raw):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        spec = freeze(spec_raw)
        spec_file = path / 'spec.json'
        if spec_file.exists():
            stored = _read_json(spec_file)
            if stored.get('digest') != spec.digest:
                raise LedgerError('账本已冻结不同方案，不能覆盖；如需调整请使用amend修订')
            return cls(path)
        _write_json(spec_file, {'spec': spec_raw, 'digest': spec.digest})
        (path / 'versions').mkdir(exist_ok=True)
        (path / 'objects').mkdir(exist_ok=True)
        (path / 'amendments.jsonl').touch()
        (path / 'decisions.jsonl').touch()
        return cls(path)

    def versions(self):
        vdir = self.path / 'versions'
        if not vdir.exists():
            return []
        return [_read_json(f) for f in sorted(vdir.glob('*.json'))]

    def _events(self):
        dfile = self.path / 'decisions.jsonl'
        if not dfile.exists():
            return []
        return [json.loads(line) for line in dfile.read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def _decisions(self):
        return [e for e in self._events() if e.get('type') == 'decision']

    def _ack_map(self):
        return {e['decision_id']: e for e in self._events() if e.get('type') == 'acknowledgment'}

    def _standing(self, before_version):
        """before_version 时点仍生效（未被确认解除）的决定。"""
        acks = self._ack_map()
        out = []
        for d in self._decisions():
            if d['version'] >= before_version:
                continue
            ack = acks.get(d['decision_id'])
            if ack and ack.get('effective_from_version', 0) <= before_version:
                continue
            out.append(d)
        return out

    def _append_version(self, rec):
        versions = self.versions()
        rec['prev_digest'] = versions[-1]['digest'] if versions else None
        rec['digest'] = content_digest(rec)
        _write_json(self.path / 'versions' / f"{rec['version']:06d}.json", rec)
        return rec

    def _store_object(self, snapshot):
        data = {'metadata': snapshot.metadata,
                'collections': {name: snapshot.rows(name) for name in SCHEMAS}}
        if content_digest(data) != snapshot.digest:
            raise LedgerError('快照对象重建失败')
        obj_file = self.path / 'objects' / f'{snapshot.digest}.json'
        if not obj_file.exists():
            _write_json(obj_file, data)

    def _conflicts(self, snapshot, standing):
        """用当前资料在历史决定的前缀长度上重算，条件不再成立即报告冲突。"""
        conflicts = []
        for d in standing:
            state = compute_cells(snapshot, self.spec, self.amendments, (),
                                  {d['cell']: d['n']})
            cur = next((c for c in state['cells'] if c['cell'] == d['cell']), None)
            if cur is None:
                conflicts.append({'decision_id': d['decision_id'], 'cell': d['cell'],
                                  'reason': 'CELL_MISSING'})
            elif cur['prefix'] != d['prefix']:
                conflicts.append({'decision_id': d['decision_id'], 'cell': d['cell'],
                                  'reason': 'PREFIX_CHANGED',
                                  'recomputed_prefix': cur['prefix']})
            elif _stop_kind(cur) != d['kind']:
                conflicts.append({'decision_id': d['decision_id'], 'cell': d['cell'],
                                  'reason': 'CONDITION_NOT_REPRODUCED',
                                  'recomputed': {'estimate': cur['estimate'],
                                                 'lower': cur['lower'],
                                                 'upper': cur['upper']}})
        return conflicts

    def record_run(self, payload, explore=()):
        """消费一份资料快照并追加版本；explore为探索性分组定义列表。"""
        explore = list(explore or ())
        versions = self.versions()
        no = len(versions) + 1
        last_compute = next((v for v in reversed(versions)
                             if v['kind'] in ('compute', 'repeat')), None)
        rec = {'version': no, 'kind': None, 'recorded_at': _now(),
               'spec_digest': self.spec.digest,
               'amendment_digests': [a.digest for a in self.amendments],
               'explore': explore,
               'input_digest': None, 'data_version': None, 'as_of': None,
               'cells': None, 'warnings': [], 'new_decisions': [],
               'conflicts': [], 'error': None}
        try:
            snapshot = catalog.load(payload, SCHEMAS)
        except (ValueError, TypeError) as exc:
            rec['kind'] = 'rejected'
            rec['error'] = f'资料修订快照无效: {exc}'
            try:
                rec['raw_digest'] = content_digest(payload)
            except (ValueError, TypeError):
                rec['raw_digest'] = None
            self._append_version(rec)
            return self._report(rec)
        rec['input_digest'] = snapshot.digest
        meta = snapshot.metadata
        rec['data_version'] = meta.get('data_version')
        try:
            as_of = meta.get('as_of')
            if (last_compute is not None and type(as_of) is int
                    and as_of < last_compute['as_of']):
                raise EngineError('as_of回退：不能用更早的逻辑时点更新账本，历史状态请用replay查看')
            state = compute_cells(snapshot, self.spec, self.amendments, explore)
        except (ValueError, TypeError, KeyError) as exc:
            rec['kind'] = 'rejected'
            rec['error'] = str(exc)
            self._append_version(rec)
            return self._report(rec)
        rec['as_of'] = state['as_of']
        kind = 'compute'
        if (last_compute is not None
                and rec['input_digest'] == last_compute['input_digest']
                and rec['amendment_digests'] == last_compute['amendment_digests']
                and canonical(rec['explore']) == canonical(last_compute.get('explore') or [])):
            kind = 'repeat'
        rec['kind'] = kind
        rec['cells'] = state['cells']
        rec['warnings'] = state['warnings']
        rec['conflicts'] = self._conflicts(snapshot, self._standing(no))
        if kind == 'compute':
            decided = {d['cell'] for d in self._decisions()}
            for cs in state['cells']:
                if cs['exploratory'] or cs['cell'] in decided:
                    continue
                stop = _stop_kind(cs)
                if stop is None:
                    continue
                rec['new_decisions'].append(
                    _decision_event(cs, no, rec['as_of'], self.spec.digest,
                                    rec['input_digest'], rec['recorded_at'], stop))
            if rec['new_decisions']:
                with (self.path / 'decisions.jsonl').open('a', encoding='utf-8') as fh:
                    for d in rec['new_decisions']:
                        fh.write(json.dumps(d, ensure_ascii=False, allow_nan=False,
                                            sort_keys=True) + '\n')
        self._store_object(snapshot)
        self._append_version(rec)
        return self._report(rec)

    def amend(self, raw):
        """追加修订：只动用剩余预算，effective_from 必须晚于已查看的as_of。"""
        versions = self.versions()
        last_as_of = None
        for v in reversed(versions):
            if v.get('as_of') is not None:
                last_as_of = v['as_of']
                break
        min_eff = self.spec.frozen_at if last_as_of is None else last_as_of + 1
        remaining = (1.0 - sum(c.weight for c in self.spec.cells)
                     - sum(c.weight for a in self.amendments for c in a.cells))
        known_m = list(self.spec.metrics) + [m for a in self.amendments for m in a.metrics]
        known_g = list(self.spec.groups) + [g for a in self.amendments for g in a.groups]
        known_keys = ({c.key for c in self.spec.cells}
                      | {c.key for a in self.amendments for c in a.cells})
        if any(a.amendment_id == raw.get('amendment_id') for a in self.amendments):
            raise LedgerError('修订id已存在')
        am, used = freeze_amendment(raw, known_m, known_g, known_keys, remaining,
                                    self.spec.alpha_total, self.spec.p_min, min_eff)
        entry = {'seq': len(self.amendments) + 1, 'amendment': raw, 'digest': am.digest}
        with (self.path / 'amendments.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, allow_nan=False,
                                sort_keys=True) + '\n')
        self.amendments.append(am)
        return {'schema_version': 1, 'amendment_id': am.amendment_id,
                'digest': am.digest, 'effective_from': am.effective_from,
                'weight_used': used,
                'remaining_weight': round(remaining - used, 12)}

    def acknowledge(self, decision_id, actor, note):
        """确认一个与当前资料冲突的历史决定；原决定记录保留不修改。"""
        if decision_id not in {d['decision_id'] for d in self._decisions()}:
            raise LedgerError(f'决定{decision_id}不存在')
        if decision_id in self._ack_map():
            raise LedgerError(f'决定{decision_id}已被确认过')
        event = {'type': 'acknowledgment', 'decision_id': decision_id,
                 'actor': actor, 'note': note, 'recorded_at': _now(),
                 'effective_from_version': len(self.versions()) + 1}
        with (self.path / 'decisions.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(event, ensure_ascii=False, allow_nan=False,
                                sort_keys=True) + '\n')
        return {'schema_version': 1, 'acknowledged': decision_id,
                'effective_from_version': event['effective_from_version']}

    def decision_log(self):
        acks = self._ack_map()
        conflict_map = {}
        for v in self.versions():
            for c in v.get('conflicts') or []:
                conflict_map.setdefault(c['decision_id'], []).append(
                    {'version': v['version'], 'reason': c['reason']})
        out = []
        for d in self._decisions():
            if d['decision_id'] in acks:
                status = 'acknowledged'
            elif d['decision_id'] in conflict_map:
                status = 'conflicted'
            else:
                status = 'standing'
            out.append({**d, 'status': status,
                        'conflicts': conflict_map.get(d['decision_id'], [])})
        return out

    def replay(self, version=None):
        """回放并重算校验：不给版本号则校验整条链。"""
        versions = self.versions()
        if version is not None:
            if type(version) is not int or not 1 <= version <= len(versions):
                raise LedgerError(f'版本{version}不存在')
            versions = versions[:version]
        details = []
        ok_all = True
        for rec in versions:
            ok, problems = self._verify(rec, versions)
            ok_all = ok_all and ok
            details.append({'version': rec['version'], 'kind': rec['kind'],
                            'verified': ok, 'problems': problems})
        report = {'schema_version': 1, 'verified': ok_all, 'details': details}
        if version is not None:
            report['record'] = versions[-1]
        return report

    def _verify(self, rec, all_versions):
        problems = []
        idx = rec['version']
        expected_prev = all_versions[idx - 2]['digest'] if idx >= 2 else None
        if rec.get('prev_digest') != expected_prev:
            problems.append('哈希链prev_digest断裂')
        body = {k: v for k, v in rec.items() if k != 'digest'}
        if content_digest(body) != rec.get('digest'):
            problems.append('版本内容哈希不匹配')
        if rec['kind'] not in ('compute', 'repeat'):
            return not problems, problems
        obj_file = self.path / 'objects' / f"{rec['input_digest']}.json"
        if not obj_file.exists():
            problems.append('输入快照对象缺失')
            return False, problems
        data = _read_json(obj_file)
        if content_digest(data) != rec['input_digest']:
            problems.append('输入快照对象哈希不匹配')
            return False, problems
        snapshot = catalog.Snapshot(data, rec['input_digest'])
        ams = [a for a in self.amendments if a.digest in set(rec['amendment_digests'])]
        state = compute_cells(snapshot, self.spec, ams, rec.get('explore') or [])
        if canonical(state['cells']) != canonical(rec['cells']):
            problems.append('单元状态重算不一致')
        if canonical(state['warnings']) != canonical(rec['warnings']):
            problems.append('警告重算不一致')
        conflicts = self._conflicts(snapshot, self._standing(idx))
        if canonical(conflicts) != canonical(rec['conflicts']):
            problems.append('冲突重算不一致')
        if rec['kind'] == 'compute':
            decided = {d['cell'] for d in self._decisions() if d['version'] < idx}
            expected = []
            for cs in state['cells']:
                if cs['exploratory'] or cs['cell'] in decided:
                    continue
                stop = _stop_kind(cs)
                if stop is None:
                    continue
                expected.append(_decision_event(cs, idx, rec['as_of'],
                                                rec['spec_digest'], rec['input_digest'],
                                                None, stop))
            got = [{k: v for k, v in d.items() if k != 'recorded_at'}
                   for d in rec['new_decisions']]
            want = [{k: v for k, v in d.items() if k != 'recorded_at'}
                    for d in expected]
            if canonical(got) != canonical(want):
                problems.append('新决定重算不一致')
        return not problems, problems

    def _budget(self):
        all_cells = list(self.spec.cells) + [c for a in self.amendments for c in a.cells]
        by_metric = {}
        by_group = {}
        for c in all_cells:
            by_metric[c.metric] = round(by_metric.get(c.metric, 0.0) + c.weight, 12)
            by_group[c.group] = round(by_group.get(c.group, 0.0) + c.weight, 12)
        allocated = sum(c.weight for c in all_cells)
        return {'alpha_total': self.spec.alpha_total,
                'allocated_weight': round(allocated, 12),
                'remaining_weight': round(max(0.0, 1.0 - allocated), 12),
                'by_metric': by_metric, 'by_group': by_group,
                'cells': [{'cell': c.key, 'weight': c.weight, 'alpha': c.alpha}
                          for c in all_cells]}

    def _report(self, rec):
        acks = self._ack_map()
        conflicts = [{**c, 'acknowledged': c['decision_id'] in acks}
                     for c in rec.get('conflicts') or []]
        return {'schema_version': 1, 'ledger_version': rec['version'],
                'kind': rec['kind'], 'spec_digest': rec['spec_digest'],
                'amendment_digests': rec['amendment_digests'],
                'input_digest': rec['input_digest'],
                'data_version': rec.get('data_version'), 'as_of': rec.get('as_of'),
                'budget': self._budget(), 'cells': rec.get('cells'),
                'warnings': rec.get('warnings'),
                'new_decisions': rec.get('new_decisions'), 'conflicts': conflicts,
                'error': rec.get('error'), 'digest': rec.get('digest')}
