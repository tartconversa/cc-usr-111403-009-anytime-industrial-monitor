# 工业试点连续查看的随时有效区间与多重决策

本项目提供随机分配概率与有界目标相关资料的现有业务摘要。当前代码执行资料修订读取与基础统计，不包含扩展分析。

app/catalog.py 按集合和记录键解析修订，拒绝同修订冲突；app/baseline.py 承载当前业务计算；app/service.py 输出摘要及输入摘要哈希。
数据字段及现有接口见 [资料契约](docs/data-contract.md)。fixtures/sample.json 是合成小数据，不对应真实企业或个人。
运行环境为 Python 3.14，现有模块仅使用标准库。

读取并计算当前摘要：

    python -m app.service fixtures/sample.json

随时有效推断（预登记方案 + 审计账本，构造与假设见 [随时有效推断](docs/anytime-valid.md)）：

    python -m app.anytime init /tmp/ledger fixtures/pilot_spec.json   # 冻结方案
    python -m app.anytime run /tmp/ledger fixtures/pilot_wave1.json  # 每周查看
    python -m app.anytime run /tmp/ledger fixtures/pilot_wave2.json
    python -m app.anytime replay /tmp/ledger --all                    # 回放校验
    python -m app.anytime decisions /tmp/ledger                       # 停止决定

运行当前模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests
