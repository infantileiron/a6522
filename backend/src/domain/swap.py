"""组别互换复核：在圈定的存疑单中找出能翻转放量结论的最少互换对。

互换只允许发生在同一分配日、同一柜点类型内一张 A 组单与一张 B 组单之间；
取件与投诉事件随单走，每张单最多参与一次互换；观察窗与全部放量门槛沿用
原运行参数。结论在「可进入放量审批」与其他状态之间翻转。组内一换一保持
各组分配数、成熟数与事件总数不变，两项 SRM 与取件检验的可用性不随互换
改变，只有各组的窗内取件数与投诉数随单移动，因此：

- 当前可放量时，任一门槛在可达范围内失守即翻转，按取件、投诉两个一维
  方向分别扫描最近失守点，再用各格可配对容量判定可达性，结果精确。
- 当前暂缓时，需要取件与投诉同时落入通过区间（二维矩形目标）。把每格的
  互换净效果表示为 A 组类型向量变化 δ（Σδ=0，受各类型存量约束，代价为
  Σ|δ|/2），按格做精确动态规划，并用贪心可行解剪枝；搜索空间以取件、
  投诉计数的变化量为轴，两百张存疑单也能给出确切结论。
"""
from collections import defaultdict
from itertools import permutations
from statistics import NormalDist

from . import analysis
from .inference import difference_interval, sample_ratio, two_proportion_test

ROLLOUT = 'rollout_candidate'
_PICKUP = (0, 0, 1, 1)
_COMPLAINT = (0, 1, 0, 1)
_BITS = {'p': _PICKUP, 'c': _COMPLAINT}
_LIMIT = 1000
_INF = float('inf')


def normalize_suspects(suspects):
    if not isinstance(suspects, (list, tuple)):
        raise ValueError('请以单号列表圈定存疑单。')
    cleaned, seen = [], set()
    for item in suspects:
        if not isinstance(item, str) or not item.strip():
            raise ValueError('存疑单号不能为空，请填写有效文本。')
        key = item.strip()
        if len(key) > 120:
            raise ValueError('存疑单号过长，请核对台账。')
        if key not in seen:
            seen.add(key)
            cleaned.append(key)
    if not cleaned:
        raise ValueError('请至少圈定一张存疑单。')
    if len(cleaned) > _LIMIT:
        raise ValueError(f'一次最多圈定 {_LIMIT} 张存疑单。')
    return cleaned


class _Context:
    """原口径下的快照、计数与门槛判定。"""

    def __init__(self, rows, params):
        self.settings = analysis._settings(params)
        self.assignments = analysis._normalize_rows(rows)
        self.cutoff = self.settings['cutoff']
        self.days = self.settings['window_days']
        overall, _cohorts, _excluded, _audit = analysis._tabulate(self.assignments, self.cutoff, self.days)
        self.a, self.b = overall['A'], overall['B']
        self.critical_z = NormalDist().inv_cdf(1 - self.settings['alpha'] / 2)
        self.original = analysis.analyze(rows, params)

    def pickup_ok(self, a_count, b_count):
        test = two_proportion_test(a_count, self.a.mature, b_count, self.b.mature)
        if test is None:  # 期望格频数条件不随互换改变，调用前已排除
            return None
        z, p = test
        interval = difference_interval(a_count, self.a.mature, b_count, self.b.mature, self.critical_z)
        return z > 0 and p < self.settings['alpha'] and interval[0] * 100 >= self.settings['min_lift_pp']

    def complaint_ok(self, a_count, b_count):
        interval = difference_interval(a_count, self.a.mature, b_count, self.b.mature, self.critical_z)
        return interval[1] * 100 <= self.settings['max_complaint_pp']

    def srm_ok(self):
        target = self.settings['target_b_pct'] / 100
        for a_size, b_size in ((self.a.assigned, self.b.assigned), (self.a.mature, self.b.mature)):
            test = sample_ratio(a_size, b_size, target)
            if test is None or test[1] < self.settings['srm_alpha']:
                return False
        return True


def _cells(ctx, suspects, by_id):
    """把成熟存疑单按（分配日, 柜点类型, 组别, 事件类型）归格；其余记录原因。"""
    excluded = []
    groups = defaultdict(lambda: {'A': defaultdict(list), 'B': defaultdict(list)})
    for suspect in suspects:
        order = by_id[suspect]
        age = (ctx.cutoff - order.assigned_on).days
        if age <= 0:
            excluded.append({'assignment_id': order.assignment_id, 'reason': '截止日及之后分配，不在快照内'})
            continue
        if age < ctx.days:
            excluded.append({'assignment_id': order.assignment_id, 'reason': '观察窗未成熟，不计入指标'})
            continue
        pickup = analysis._in_window(order.assigned_on, order.picked_up_on, ctx.cutoff, ctx.days)
        complaint = analysis._in_window(order.assigned_on, order.complained_on, ctx.cutoff, ctx.days)
        kind = 2 * int(pickup) + int(complaint)
        groups[(order.assigned_on.isoformat(), order.cohort)][order.arm][kind].append(order.assignment_id)
    cells = []
    for assigned_on, cohort in sorted(groups):
        arms = groups[(assigned_on, cohort)]
        cells.append({
            'assigned_on': assigned_on,
            'cohort': cohort,
            'A': [tuple(sorted(arms['A'][kind])) for kind in range(4)],
            'B': [tuple(sorted(arms['B'][kind])) for kind in range(4)],
        })
    return cells, excluded


def _bit_capacity(cells, kind, a_bit, b_bit):
    """各格内可把 A 组某类单与 B 组某类单配对的最大对数之和。"""
    bits = _BITS[kind]
    total = 0
    for cell in cells:
        a_side = sum(len(cell['A'][t]) for t in range(4) if bits[t] == a_bit)
        b_side = sum(len(cell['B'][t]) for t in range(4) if bits[t] == b_bit)
        total += min(a_side, b_side)
    return total


def _reach(ctx, cells, kind):
    """A 组窗内计数（取件或投诉）在存疑单互换下的可达区间。"""
    start = getattr(ctx.a, 'picked_up' if kind == 'p' else 'complained')
    total = start + getattr(ctx.b, 'picked_up' if kind == 'p' else 'complained')
    plus = _bit_capacity(cells, kind, 0, 1)
    minus = _bit_capacity(cells, kind, 1, 0)
    low = max(0, start - minus, total - ctx.b.mature)
    high = min(ctx.a.mature, start + plus, total)
    return low, high


def _shift_pairs(cells, kind, delta):
    """按一个方向逐格配对，使 A 组计数净增 delta（每对恰改变 1）。"""
    a_bit, b_bit = (0, 1) if delta > 0 else (1, 0)
    need = abs(delta)
    pairs = []
    for index, cell in enumerate(cells):
        if need == 0:
            break
        a_orders = [order for t in range(4) if _BITS[kind][t] == a_bit for order in cell['A'][t]]
        b_orders = [order for t in range(4) if _BITS[kind][t] == b_bit for order in cell['B'][t]]
        take = min(need, len(a_orders), len(b_orders))
        pairs.extend((index, a_id, b_id) for a_id, b_id in zip(a_orders[:take], b_orders[:take]))
        need -= take
    if need:
        raise ValueError('可互换的存疑单不足，无法达成翻转。')
    return pairs


def _flip_out(ctx, cells):
    """当前可放量：找任一门槛在可达范围内的最近失守点。"""
    options = []
    reach = {}
    totals = {'p': ctx.a.picked_up + ctx.b.picked_up, 'c': ctx.a.complained + ctx.b.complained}
    starts = {'p': ctx.a.picked_up, 'c': ctx.a.complained}
    checks = {'p': ctx.pickup_ok, 'c': ctx.complaint_ok}
    for kind in ('p', 'c'):
        low, high = _reach(ctx, cells, kind)
        reach[kind] = (low, high)
        start, total, check = starts[kind], totals[kind], checks[kind]
        for value in range(low, high + 1):
            if check(value, total - value) is False:
                options.append((abs(value - start), kind, value - start))
    evidence = {'pickup': list(reach['p']), 'complaint': list(reach['c'])}
    if not options:
        reason = (f"在可达范围（A 组窗内取件 {reach['p'][0]}–{reach['p'][1]} 单、"
                  f"窗内投诉 {reach['c'][0]}–{reach['c'][1]} 单）内，取件改善、实际收益与投诉护栏门槛仍全部通过。")
        return {'ok': False, 'reach': evidence, 'reason': reason}
    _distance, kind, delta = min(options)
    return {'ok': True, 'pairs': _shift_pairs(cells, kind, delta)}


def _type_index(p_bit, c_bit):
    return 2 * int(p_bit) + int(c_bit)


def _cell_moves(a_supply, b_supply):
    """一格内 A 组类型向量的可行净变化：(取件差, 投诉差) -> (代价, δ)。

    δ[t] 为 A 组类型 t 的净增量，Σδ=0，下界为 -A 组该类存量、上界为 B 组
    该类存量；代价 Σ|δ|/2 即互换对数。该刻画与逐对运输约束等价。
    """
    moves = {}
    for d0 in range(-a_supply[0], b_supply[0] + 1):
        for d1 in range(-a_supply[1], b_supply[1] + 1):
            for d2 in range(-a_supply[2], b_supply[2] + 1):
                d3 = -(d0 + d1 + d2)
                if not -a_supply[3] <= d3 <= b_supply[3]:
                    continue
                key = (d2 + d3, d1 + d3)
                cost = (abs(d0) + abs(d1) + abs(d2) + abs(d3)) // 2
                if key not in moves or cost < moves[key][0]:
                    moves[key] = (cost, (d0, d1, d2, d3))
    return moves


def _greedy(cells, need_p, need_c):
    """构造可达矩形目标的互换方案，返回 (代价, 配对) 或 None；仅作上界。"""
    best = None
    for phases in permutations('dpc'):
        attempt = _greedy_attempt(cells, need_p, need_c, phases)
        if attempt and (best is None or attempt[0] < best[0]):
            best = attempt
    return best


def _greedy_attempt(cells, need_p, need_c, phases):
    pointers = [([0] * 4, [0] * 4) for _ in cells]
    remaining = {'p': need_p, 'c': need_c}
    pairs = []

    def apply(x, y, want):
        moved = 0
        for index, cell in enumerate(cells):
            if moved >= want:
                break
            a_ptr, b_ptr = pointers[index]
            available = min(len(cell['A'][x]) - a_ptr[x], len(cell['B'][y]) - b_ptr[y], want - moved)
            for _ in range(available):
                pairs.append((index, cell['A'][x][a_ptr[x]], cell['B'][y][b_ptr[y]]))
                a_ptr[x] += 1
                b_ptr[y] += 1
            moved += available
        remaining['p'] -= moved * (_PICKUP[y] - _PICKUP[x])
        remaining['c'] -= moved * (_COMPLAINT[y] - _COMPLAINT[x])

    for phase in phases:
        if phase == 'd':
            if remaining['p'] and remaining['c']:
                x = _type_index(remaining['p'] < 0, remaining['c'] < 0)
                y = _type_index(remaining['p'] > 0, remaining['c'] > 0)
                apply(x, y, min(abs(remaining['p']), abs(remaining['c'])))
        elif remaining[phase]:
            positive = remaining[phase] > 0
            for bit in (0, 1):
                if phase == 'p':
                    x, y = _type_index(not positive, bit), _type_index(positive, bit)
                else:
                    x, y = _type_index(bit, not positive), _type_index(bit, positive)
                apply(x, y, abs(remaining[phase]))
    if remaining['p'] or remaining['c']:
        return None
    return len(pairs), pairs


def _search(cells, origin, sets, reach, best):
    """精确动态规划：达到通过矩形的最少互换对数及逐格净变化。"""
    origin_p, origin_c = origin
    set_p, set_c = sets
    (low_p, high_p), (low_c, high_c) = reach
    dist_p = {v: 0 if v in set_p else min(abs(v - u) for u in set_p) for v in range(low_p, high_p + 1)}
    dist_c = {v: 0 if v in set_c else min(abs(v - u) for u in set_c) for v in range(low_c, high_c + 1)}
    moves, cache = [], {}
    for cell in cells:
        signature = (tuple(len(cell['A'][t]) for t in range(4)),
                     tuple(len(cell['B'][t]) for t in range(4)))
        if signature not in cache:
            cache[signature] = _cell_moves(*signature)
        moves.append(cache[signature])
    usable = sorted((i for i in range(len(cells)) if len(moves[i]) > 1), key=lambda i: (len(moves[i]), i))
    dp = {(0, 0): 0}
    layers = []
    for i in usable:
        parent = {}
        following = dict(dp)
        for (done_p, done_c), cost in dp.items():
            for (move_p, move_c), (move_cost, delta) in moves[i].items():
                if move_cost == 0:
                    continue
                new_cost = cost + move_cost
                if best is not None and new_cost >= best:
                    continue
                state = (done_p + move_p, done_c + move_c)
                if best is not None and new_cost + max(
                        dist_p[origin_p + state[0]], dist_c[origin_c + state[1]]) >= best:
                    continue
                if new_cost < following.get(state, _INF):
                    following[state] = new_cost
                    parent[state] = ((done_p, done_c), i, delta)
        dp = following
        layers.append((i, parent))
    target, answer = None, None
    for (done_p, done_c), cost in dp.items():
        if best is not None and cost >= best:
            continue
        if origin_p + done_p in set_p and origin_c + done_c in set_c:
            if answer is None or cost < answer:
                target, answer = (done_p, done_c), cost
    if target is None:
        return None
    chosen = {}
    state = target
    for i, parent in reversed(layers):
        if state in parent:
            previous, cell_index, delta = parent[state]
            chosen[cell_index] = delta
            state = previous
    return answer, chosen


def _realize(cells, chosen):
    """把逐格净变化落成具体单号配对：A 净流出类型与 B 净流出类型一一配对。"""
    pairs = []
    for index, delta in sorted(chosen.items()):
        cell = cells[index]
        outgoing, incoming = [], []
        for t in range(4):
            if delta[t] < 0:
                outgoing.extend(cell['A'][t][:-delta[t]])
            elif delta[t] > 0:
                incoming.extend(cell['B'][t][:delta[t]])
        pairs.extend((index, a_id, b_id) for a_id, b_id in zip(outgoing, incoming))
    return pairs


def _shift_toward(value, pass_set):
    if value in pass_set:
        return 0
    nearest = min(pass_set, key=lambda item: (abs(item - value), item))
    return nearest - value


def _flip_in(ctx, cells):
    """当前暂缓：需同时满足取件与投诉门槛，求达矩形目标的最少互换。"""
    if not ctx.srm_ok():
        return {'ok': False,
                'reason': '分流完整性检查不随组内互换改变（一换一保持各组分配数与成熟数不变），当前未通过。'}
    if two_proportion_test(ctx.a.picked_up, ctx.a.mature, ctx.b.picked_up, ctx.b.mature) is None:
        return {'ok': False,
                'reason': '取件检验的期望格频数条件不随互换改变，当前检验不可用。'}
    reach_p = _reach(ctx, cells, 'p')
    reach_c = _reach(ctx, cells, 'c')
    total_p = ctx.a.picked_up + ctx.b.picked_up
    total_c = ctx.a.complained + ctx.b.complained
    set_p = {v for v in range(reach_p[0], reach_p[1] + 1) if ctx.pickup_ok(v, total_p - v)}
    set_c = {v for v in range(reach_c[0], reach_c[1] + 1) if ctx.complaint_ok(v, total_c - v)}
    evidence = {'pickup': list(reach_p), 'complaint': list(reach_c)}
    if not set_p or not set_c:
        return {'ok': False, 'reach': evidence,
                'reason': '即使把可互换的存疑单全部推向有利方向，取件或投诉门槛仍无法通过。'}
    need_p = _shift_toward(ctx.a.picked_up, set_p)
    need_c = _shift_toward(ctx.a.complained, set_c)
    greedy = _greedy(cells, need_p, need_c)
    if greedy and greedy[0] == max(abs(need_p), abs(need_c)):
        return {'ok': True, 'pairs': greedy[1]}
    found = _search(cells, (ctx.a.picked_up, ctx.a.complained), (set_p, set_c),
                    (reach_p, reach_c), greedy[0] if greedy else None)
    if found:
        return {'ok': True, 'pairs': _realize(cells, found[1])}
    if greedy:
        return {'ok': True, 'pairs': greedy[1]}
    return {'ok': False, 'reach': evidence,
            'need': {'pickup': [min(set_p), max(set_p)], 'complaint': [min(set_c), max(set_c)]},
            'reason': '存疑范围内不存在同时满足取件与投诉门槛的互换组合。'}


def _pair_rows(ctx, by_id, raw_pairs):
    rows = []
    for _index, a_id, b_id in raw_pairs:
        a_order, b_order = by_id[a_id], by_id[b_id]
        rows.append({
            'assigned_on': a_order.assigned_on.isoformat(),
            'cohort': a_order.cohort,
            'a_id': a_id,
            'b_id': b_id,
            'a_pickup': analysis._in_window(a_order.assigned_on, a_order.picked_up_on, ctx.cutoff, ctx.days),
            'a_complaint': analysis._in_window(a_order.assigned_on, a_order.complained_on, ctx.cutoff, ctx.days),
            'b_pickup': analysis._in_window(b_order.assigned_on, b_order.picked_up_on, ctx.cutoff, ctx.days),
            'b_complaint': analysis._in_window(b_order.assigned_on, b_order.complained_on, ctx.cutoff, ctx.days),
        })
    rows.sort(key=lambda item: (item['assigned_on'], item['cohort'], item['a_id'], item['b_id']))
    return rows


def _apply(rows, pairs):
    swapped = [dict(row) for row in rows]
    by_id = {row['assignment_id']: row for row in swapped}
    for pair in pairs:
        first, second = by_id[pair['a_id']], by_id[pair['b_id']]
        first['arm'], second['arm'] = second['arm'], first['arm']
    return swapped


def review_swaps(rows, params, suspects):
    """复核圈定的存疑单：返回最少互换对方案或无法翻转的确切说明。"""
    suspects = normalize_suspects(suspects)
    ctx = _Context(rows, params)
    by_id = {order.assignment_id: order for order in ctx.assignments}
    unknown = [key for key in suspects if key not in by_id]
    if unknown:
        shown = '、'.join(unknown[:5])
        suffix = ' 等' if len(unknown) > 5 else ''
        raise ValueError(f'存疑单号在该数据批次中不存在：{shown}{suffix}。')
    cells, excluded = _cells(ctx, suspects, by_id)
    original_state = ctx.original['state']
    outcome = _flip_out(ctx, cells) if original_state == ROLLOUT else _flip_in(ctx, cells)
    base = {
        'suspect_count': len(suspects),
        'mature_count': sum(len(cell['A'][t]) + len(cell['B'][t]) for cell in cells for t in range(4)),
        'excluded': excluded,
        'original': {'state': original_state, 'headline': ctx.original['headline']},
        'reach': outcome.get('reach'),
    }
    note = f'{ctx.cutoff.isoformat()} 零点快照 · {ctx.days} 天观察窗 · 沿用原观察窗与全部门槛。'
    if not outcome['ok']:
        return {**base, 'state': 'impossible', 'headline': '存疑范围内无法翻转放量结论',
                'note': note, 'min_swaps': None, 'pairs': [], 'after': None,
                'reason': outcome['reason'], 'need': outcome.get('need')}
    pairs = _pair_rows(ctx, by_id, outcome['pairs'])
    after = analysis.analyze(_apply(rows, pairs), params)
    if (after['state'] == ROLLOUT) == (original_state == ROLLOUT):
        raise ValueError('互换方案复核失败，请重新圈定存疑单。')
    count = len(pairs)
    return {**base, 'state': 'flipped',
            'headline': f'最少 {count} 对互换即可翻转放量结论',
            'note': f'{note}取件与投诉随单走，每单最多互换一次。',
            'min_swaps': count, 'pairs': pairs, 'reason': None, 'need': None,
            'after': {'state': after['state'], 'headline': after['headline'],
                      'metrics': after['metrics'], 'gates': after['tables'][0]['rows']}}
