"""补样名额分配：二部图最大匹配（Kuhn 增广路算法）。

左侧是各专题的缺额（每个缺额一个槽位），右侧是备选发票；
票在某专题备选名单内即有一条边。每张票最多占一个名额，
算法保证本批补上的名额总数最大，且在给定次序下结果确定：
专题按登记顺序处理，每个专题的备选按登记顺序尝试。
"""
from __future__ import annotations


def allocate(topic_order, vacancies, candidates):
    """返回 {topic_code: [分得票号, ...]}（按备选登记顺序排列）。

    topic_order: 专题代码列表（登记顺序）
    vacancies:   {topic_code: 待补名额数}
    candidates:  {topic_code: [可用备选票号, ...]}（登记顺序）
    """
    owner = {}  # 票号 -> 占用它的专题

    def augment(topic, seen):
        for inv in candidates.get(topic, ()):
            if inv in seen:
                continue
            seen.add(inv)
            prev = owner.get(inv)
            if prev is None or augment(prev, seen):
                owner[inv] = topic
                return True
        return False

    for topic in topic_order:
        for _ in range(max(0, vacancies.get(topic, 0))):
            if not augment(topic, set()):
                break

    result = {t: [] for t in topic_order}
    for topic in topic_order:
        for inv in candidates.get(topic, ()):
            if owner.get(inv) == topic:
                result[topic].append(inv)
    return result
