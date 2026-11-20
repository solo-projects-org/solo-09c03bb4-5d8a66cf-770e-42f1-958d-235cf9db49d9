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


def deficiency_group(topic_order, vacancies, candidates):
    """核查用：找出“备选不足”的专题组，并给出最大可补数与未匹配槽位数。

    返回 (group_topics, unmatched_count, max_fillable)：
    - group_topics 按登记顺序，是一组专题，满足
      Σ 补前缺额 - 该组备选票号并集大小 == unmatched_count（全轮剩余缺额）。
    - unmatched_count 是该二部图最大匹配下补不上的槽位数。
    - max_fillable 是这份备选池最多能补上的名额数。

    做法：左部每个缺额一个槽位，先求最大匹配；再从所有未匹配槽位出发沿交替路径
    （未匹配边 -> 备选 -> 匹配边 -> 槽位）做可达标记。由最大匹配性质，从任一未匹配
    槽位出发不存在增广路，故可达备选均已匹配、其匹配槽位也均可达，于是
    可达槽位数 - 可达备选数 == 未匹配槽位数；把这些槽位所属专题作为专题组即得等式。
    这与补样（allocate）使用的是同一张图，结果不依赖具体匹配方案。
    """
    slots = []  # 槽位 -> 专题
    for topic in topic_order:
        slots.extend([topic] * max(0, vacancies.get(topic, 0)))
    total_slots = len(slots)
    if total_slots == 0:
        return [], 0, 0

    match_slot = [-1] * total_slots  # 槽位 -> 匹配到的票号
    owner = {}  # 票号 -> 槽位

    def augment(slot, seen):
        for inv in candidates.get(slots[slot], ()):
            if inv in seen:
                continue
            seen.add(inv)
            prev = owner.get(inv)
            if prev is None or augment(prev, seen):
                owner[inv] = slot
                match_slot[slot] = inv
                return True
        return False

    for slot in range(total_slots):
        augment(slot, set())

    unmatched = [s for s in range(total_slots) if match_slot[s] == -1]
    if not unmatched:
        return [], 0, total_slots

    # 从未匹配槽位出发的交替可达集（槽位在左、备选在右）
    reachable_slots = set(unmatched)
    reachable_cands = set()
    stack = list(unmatched)
    while stack:
        slot = stack.pop()
        for inv in candidates.get(slots[slot], ()):
            if inv in reachable_cands:
                continue
            reachable_cands.add(inv)
            nxt = owner.get(inv)
            if nxt is not None and nxt not in reachable_slots:
                reachable_slots.add(nxt)
                stack.append(nxt)

    group = [t for t in topic_order if any(slots[s] == t for s in reachable_slots)]
    return group, len(unmatched), total_slots - len(unmatched)
