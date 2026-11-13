"""现有业务摘要，不推断未观察到的业务关系。"""
def analyze(snapshot):
    cutoff=snapshot.metadata['as_of'];pmin=snapshot.metadata['p_min'];low,high=snapshot.metadata['bounds']
    outcomes={r['enterprise']:r for r in snapshot.rows('outcomes') if r['known']<=cutoff};result=[];seen=set()
    for a in sorted(snapshot.rows('assignments'),key=lambda r:r['order']):
        if a['order'] in seen:raise ValueError('企业顺序重复')
        seen.add(a['order']);p=a['probability']
        if not pmin<=p<=1-pmin:raise ValueError('分配概率越界')
        o=outcomes.get(a['id'])
        if a['registered']>cutoff or o is None:break
        if o['known']<a['registered']:raise ValueError('结果早于登记')
        y=min(high,max(low,o['value']));score=y/p if a['treated'] else -y/(1-p)
        result.append({'enterprise':a['id'],'bounded_value':y,'score':score})
    return result
