# 资料与现有计算约定

## 修订快照

schema_version为整数1，metadata为业务方案字典，collections为业务集合字典。每条记录有非空字符串id和正整数revision。
同集合相同id取最高revision；同id同revision内容不同即冲突，即便存在更高修订也拒绝。deleted=true为撤回，之后更高修订可恢复。
撤回记录不要求携带业务字段。输入顺序与相同记录重试不改变语义；非有限数字拒绝。
Snapshot.rows返回独立副本；input_digest绑定有效修订和全部metadata。输出data_version沿用metadata，不由程序自造资料版本。
既有摘要入口为app.service.inspect(payload)，输出schema_version、input_digest、data_version和summary。

## 业务资料

assignments含order、treated、probability、registered；outcomes含enterprise、value、known。order唯一，企业键由assignment.id给出；概率在[p_min,1-p_min]，known>=registered。target为bounded outcome的ATE，观测得分treated*y/p-(1-treated)*y/(1-p)，不除当前已观测组人数。baseline.analyze只返回as_of内已知结果的连续企业前缀与得分，不作推断。metadata.bounds是截顶范围。

各集合必要字段：{"assignments": ["order", "treated", "probability", "registered"], "outcomes": ["enterprise", "value", "known"]}。

## 当前能力

app.baseline.analyze(snapshot)产生已有摘要，app.service.inspect负责绑定输入身份；新增分析可以增设独立入口，已有接口及上述数据语义保持兼容。

## 随时有效推断扩展

assignments记录可带可选attributes对象（字符串键到字符串/数值），供预登记分组匹配；outcomes记录可带可选metric字段，单指标方案下缺省归入该指标，多指标方案下缺省不纳入任何单元。两字段缺省时语义与既有资料完全一致。

app.anytime提供随时有效推断账本：init冻结预登记方案（指标截顶范围、分组、单元权重预算、总量alpha），run消费资料快照产生版本化报告（区间、检验证据、有效前缀、阻塞原因），replay重算并校验任意版本，amend只用剩余预算追加前向生效的修订，acknowledge确认与更正资料冲突的历史决定。构造、假设与审计规则见docs/anytime-valid.md。
