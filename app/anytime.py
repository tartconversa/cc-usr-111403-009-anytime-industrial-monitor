"""随时有效推断账本命令行：冻结方案、运行查看、回放、修订与确认。

用法：
  python -m app.anytime init LEDGER spec.json            冻结预登记方案
  python -m app.anytime run LEDGER payload.json [--explore explore.json]
  python -m app.anytime replay LEDGER [N | --all]        回放并校验版本
  python -m app.anytime decisions LEDGER                 查看决定与状态
  python -m app.anytime amend LEDGER amendment.json      追加修订（只用剩余预算）
  python -m app.anytime acknowledge LEDGER DECISION_ID --actor 谁 --note 原因
"""
import json
import sys
from pathlib import Path

from .ledger import Ledger, LedgerError
from .spec import SpecError

USAGE = __doc__


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=2))


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _flag(args, name, default=None):
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        raise SystemExit(f'{name}缺少参数')
    value = args[i + 1]
    del args[i:i + 2]
    return value


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit(USAGE)
    cmd = args.pop(0)
    try:
        if cmd == 'init':
            if len(args) != 2:
                raise SystemExit('init需要账本目录与方案JSON路径')
            ledger = Ledger.init(args[0], _load(args[1]))
            _emit({'schema_version': 1, 'frozen': True, 'spec_digest': ledger.spec.digest})
        elif cmd == 'run':
            explore_path = _flag(args, '--explore')
            if len(args) != 2:
                raise SystemExit('run需要账本目录与资料JSON路径')
            explore = _load(explore_path) if explore_path else []
            if isinstance(explore, dict):
                explore = [explore]
            _emit(Ledger(args[0]).record_run(_load(args[1]), explore))
        elif cmd == 'replay':
            if len(args) != 2:
                raise SystemExit('replay需要账本目录与版本号或--all')
            ledger = Ledger(args[0])
            _emit(ledger.replay() if args[1] == '--all' else ledger.replay(int(args[1])))
        elif cmd == 'decisions':
            if len(args) != 1:
                raise SystemExit('decisions需要账本目录')
            _emit({'schema_version': 1, 'decisions': Ledger(args[0]).decision_log()})
        elif cmd == 'amend':
            if len(args) != 2:
                raise SystemExit('amend需要账本目录与修订JSON路径')
            _emit(Ledger(args[0]).amend(_load(args[1])))
        elif cmd == 'acknowledge':
            actor = _flag(args, '--actor')
            note = _flag(args, '--note', '')
            if len(args) != 2 or not actor:
                raise SystemExit('acknowledge需要账本目录、决定ID与--actor')
            _emit(Ledger(args[0]).acknowledge(args[1], actor, note))
        else:
            raise SystemExit(f'未知命令{cmd}\n{USAGE}')
    except (LedgerError, SpecError, ValueError, FileNotFoundError) as exc:
        raise SystemExit(f'错误: {exc}')


if __name__ == '__main__':
    main()
