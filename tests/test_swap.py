"""组别互换复核求解器的独立验证。

运行：python3 -I tests/test_swap.py
"""
import sys
import time
from collections import defaultdict
from itertools import combinations, product
from pathlib import Path
from random import Random
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))

from src.domain import analysis
from src.domain.demo import demo_rows
from src.domain.inference import difference_interval, sample_ratio, two_proportion_test
from src.domain.swap import _cell_moves, _greedy, _search, normalize_suspects, review_swaps

ROLLOUT = 'rollout_candidate'
FAILURES = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok · {name}')
    else:
        FAILURES.append(name)
        print(f'  FAIL · {name} {detail}')


def apply_pairs(rows, pairs):
    swapped = [dict(row) for row in rows]
    by_id = {row['assignment_id']: row for row in swapped}
    for a_id, b_id in pairs:
        first, second = by_id[a_id], by_id[b_id]
        first['arm'], second['arm'] = second['arm'], first['arm']
    return swapped


def brute_min_swaps(rows, params, suspects, k_max):
    """独立暴力：按对数枚举所有合法互不相交互换组合，返回翻转所需最少对数。"""
    settings = analysis._settings(params)
    assignments = analysis._normalize_rows(rows)
    cutoff, days = settings['cutoff'], settings['window_days']
    by_id = {item.assignment_id: item for item in assignments}
    original = analysis.analyze(rows, params)['state'] == ROLLOUT
    mature = {}
    for sid in dict.fromkeys(suspects):
        order = by_id[sid]
        age = (cutoff - order.assigned_on).days
        if 0 < age >= days:
            mature[sid] = order
    pairs = []
    ids = sorted(mature)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            x, y = mature[ids[i]], mature[ids[j]]
            if x.arm != y.arm and x.assigned_on == y.assigned_on and x.cohort == y.cohort:
                pairs.append((x.assignment_id, y.assignment_id))
    for k in range(1, min(k_max, len(pairs)) + 1):
        for combo in combinations(pairs, k):
            used = set()
            for a_id, b_id in combo:
                if a_id in used or b_id in used:
                    break
                used.update((a_id, b_id))
            else:
                after = analysis.analyze(apply_pairs(rows, combo), params)['state']
                if (after == ROLLOUT) != original:
                    return k
    return None


def random_rows(rng, n_cells, lo, hi):
    rows = []
    cohorts = ['住宅柜', '园区柜', '交通站柜']
    number = 0
    for _ in range(n_cells):
        cohort = rng.choice(cohorts)
        from datetime import date, timedelta
        assigned = date(2026, 11, 9) + timedelta(days=rng.randrange(0, 5))
        for arm in ('A', 'B'):
            for _ in range(rng.randrange(lo, hi + 1)):
                number += 1
                rows.append({
                    'assignment_id': f'T-{number:03d}',
                    'recipient_id': f'R-{number:03d}',
                    'cohort': cohort,
                    'arm': arm,
                    'assigned_on': assigned.isoformat(),
                    'delivered_on': assigned.isoformat(),
                    'picked_up_on': (assigned + timedelta(days=rng.randrange(0, 3))).isoformat()
                    if rng.random() < 0.7 else None,
                    'complained_on': (assigned + timedelta(days=1)).isoformat()
                    if rng.random() < 0.12 else None,
                })
    return rows


def check_pairs_valid(result, suspects, rows):
    by_id = {row['assignment_id']: row for row in rows}
    used = set()
    scope = set(suspects)
    for pair in result['pairs']:
        a_row, b_row = by_id[pair['a_id']], by_id[pair['b_id']]
        assert a_row['arm'] == 'A' and b_row['arm'] == 'B', '配对方向有误'
        assert a_row['assigned_on'] == b_row['assigned_on'] == pair['assigned_on'], '非同分配日'
        assert a_row['cohort'] == b_row['cohort'] == pair['cohort'], '非同类柜点'
        assert pair['a_id'] in scope and pair['b_id'] in scope, '超出圈定范围'
        assert pair['a_id'] not in used and pair['b_id'] not in used, '单号被重复使用'
        used.update((pair['a_id'], pair['b_id']))
    return len(used) == 2 * result['min_swaps']


def test_baseline():
    print('基线：演示台账原结论')
    result = analysis.analyze(demo_rows(), {})
    check('演示台账原结论为可放量', result['state'] == ROLLOUT, result['state'])


def test_demo_flip_out():
    print('演示台账：圈定 200 张存疑单求最少互换对')
    rows = demo_rows()
    rng = Random(20261120)
    suspects = rng.sample([row['assignment_id'] for row in rows[:1200]], 200)
    started = time.perf_counter()
    result = review_swaps(rows, {}, suspects)
    elapsed = time.perf_counter() - started
    check('给出确切结论', result['state'] in ('flipped', 'impossible'))
    print(f'    结论：{result["headline"]}（{elapsed:.3f}s）')
    check('200 张存疑单秒级出结论', elapsed < 5)
    if result['state'] != 'flipped':
        return
    check('方案对数与最少对数一致', len(result['pairs']) == result['min_swaps'])
    check('互换对全部合法', check_pairs_valid(result, suspects, rows))
    after = analysis.analyze(apply_pairs(rows, [(p['a_id'], p['b_id']) for p in result['pairs']]), {})
    check('按方案互换后结论翻转', (after['state'] == ROLLOUT) is False, after['state'])
    # 独立下界：每个门槛最近失守距离（每对互换对单坐标至多改变 1）
    settings = analysis._settings({})
    assignments = analysis._normalize_rows(rows)
    overall, *_ = analysis._tabulate(assignments, settings['cutoff'], settings['window_days'])
    a, b = overall['A'], overall['B']
    cz = NormalDist().inv_cdf(1 - settings['alpha'] / 2)

    def pickup_ok(v):
        test = two_proportion_test(v, a.mature, a.picked_up + b.picked_up - v, b.mature)
        z, p = test
        ci = difference_interval(v, a.mature, a.picked_up + b.picked_up - v, b.mature, cz)
        return z > 0 and p < settings['alpha'] and ci[0] * 100 >= settings['min_lift_pp']

    def complaint_ok(v):
        ci = difference_interval(v, a.mature, a.complained + b.complained - v, b.mature, cz)
        return ci[1] * 100 <= settings['max_complaint_pp']

    lo_p = max(0, a.picked_up + b.picked_up - b.mature)
    hi_p = min(a.mature, a.picked_up + b.picked_up)
    lo_c = max(0, a.complained + b.complained - b.mature)
    hi_c = min(a.mature, a.complained + b.complained)
    d_p = min(abs(v - a.picked_up) for v in range(lo_p, hi_p + 1) if not pickup_ok(v))
    d_c = min(abs(v - a.complained) for v in range(lo_c, hi_c + 1) if not complaint_ok(v))
    check('不少于独立下界', result['min_swaps'] >= min(d_p, d_c),
          f"k={result['min_swaps']} 下界={min(d_p, d_c)}")


def test_demo_flip_in():
    print('演示台账：抬高收益门槛构造暂缓，再验证翻回放量的最少互换')
    rows = demo_rows()
    params = {'min_lift_pp': 9}
    original = analysis.analyze(rows, params)
    check('抬高门槛后原结论为暂缓', original['state'] != ROLLOUT, original['state'])
    settings = analysis._settings(params)
    assignments = analysis._normalize_rows(rows)
    cutoff, days = settings['cutoff'], settings['window_days']
    suspects = []
    for order in assignments:
        age = (cutoff - order.assigned_on).days
        if not 0 < age >= days:
            continue
        pickup = analysis._in_window(order.assigned_on, order.picked_up_on, cutoff, days)
        if (order.arm == 'A' and pickup) or (order.arm == 'B' and not pickup):
            suspects.append(order.assignment_id)
    check('圈定规模在限额内', len(suspects) <= 1000, str(len(suspects)))
    result = review_swaps(rows, params, suspects)
    print(f'    结论：{result["headline"]}')
    check('翻回放量', result['state'] == 'flipped')
    check('互换后结论为可放量', result['after']['state'] == ROLLOUT)
    check('互换对全部合法', check_pairs_valid(result, suspects, rows))
    # 独立期望值：取件门槛通过的最大 A 组取件数
    overall, *_ = analysis._tabulate(assignments, cutoff, days)
    a, b = overall['A'], overall['B']
    cz = NormalDist().inv_cdf(1 - settings['alpha'] / 2)

    def pickup_ok(v):
        test = two_proportion_test(v, a.mature, a.picked_up + b.picked_up - v, b.mature)
        z, p = test
        ci = difference_interval(v, a.mature, a.picked_up + b.picked_up - v, b.mature, cz)
        return z > 0 and p < settings['alpha'] and ci[0] * 100 >= settings['min_lift_pp']

    lo_p = max(0, a.picked_up + b.picked_up - b.mature)
    hi_p = min(a.mature, a.picked_up + b.picked_up)
    passing = [v for v in range(lo_p, hi_p + 1) if pickup_ok(v)]
    need = a.picked_up - max(passing)
    check('最少对数等于独立期望', result['min_swaps'] == need,
          f"k={result['min_swaps']} 期望={need}")


def test_brute_force_small():
    print('小规模随机台账：与暴力枚举逐一对比')
    rng = Random(261118)
    verified = skipped = 0
    for case in range(120):
        rows = random_rows(rng, rng.randrange(2, 4), 1, 3)
        ids = [row['assignment_id'] for row in rows]
        suspects = rng.sample(ids, rng.randrange(max(1, len(ids) // 2), len(ids) + 1))
        result = review_swaps(rows, {}, suspects)
        brute = brute_min_swaps(rows, {}, suspects, k_max=6)
        if brute is None and result['state'] == 'flipped' and result['min_swaps'] > 6:
            skipped += 1
            continue
        expect = 'flipped' if brute is not None else 'impossible'
        assert result['state'] == expect, f'第 {case} 例：求解器 {result["state"]}，暴力 {brute}'
        if brute is not None:
            assert result['min_swaps'] == brute, f'第 {case} 例：求解器 {result["min_swaps"]}，暴力 {brute}'
            assert check_pairs_valid(result, suspects, rows)
        verified += 1
    check('120 例全部与暴力一致（跳过超界情形）', verified >= 100, f'验证 {verified}，跳过 {skipped}')


def test_brute_force_random_params():
    print('随机门槛与观察窗：与暴力枚举逐一对比')
    rng = Random(802261)
    verified = skipped = 0
    for case in range(120):
        rows = random_rows(rng, rng.randrange(2, 4), 1, 3)
        params = {
            'window_days': rng.choice([2, 3, 4]),
            'alpha': rng.choice([0.01, 0.05, 0.1]),
            'srm_alpha': rng.choice([0.001, 0.01]),
            'min_lift_pp': rng.choice([0, 3, 8, 15]),
            'max_complaint_pp': rng.choice([1, 2, 10]),
            'target_b_pct': rng.choice([40, 50, 60]),
        }
        ids = [row['assignment_id'] for row in rows]
        suspects = rng.sample(ids, rng.randrange(max(1, len(ids) // 2), len(ids) + 1))
        result = review_swaps(rows, params, suspects)
        brute = brute_min_swaps(rows, params, suspects, k_max=6)
        if brute is None and result['state'] == 'flipped' and result['min_swaps'] > 6:
            skipped += 1
            continue
        expect = 'flipped' if brute is not None else 'impossible'
        assert result['state'] == expect, \
            f'第 {case} 例 {params}：求解器 {result["state"]}，暴力 {brute}'
        if brute is not None:
            assert result['min_swaps'] == brute, \
                f'第 {case} 例 {params}：求解器 {result["min_swaps"]}，暴力 {brute}'
            assert check_pairs_valid(result, suspects, rows)
        verified += 1
    check('120 例随机参数全部与暴力一致', verified >= 100, f'验证 {verified}，跳过 {skipped}')


def test_cell_moves_and_search():
    print('格子净变化与二维搜索：与匹配枚举对比')
    rng = Random(11803)

    def matchings(na, nb):
        """独立枚举一格内全部互不相交配对，得到 (取件差, 投诉差) -> 最小对数。"""
        out = {}
        types = range(4)
        arcs = [(x, y) for x in types for y in types if x != y]
        for choices in product(range(3), repeat=len(arcs)):
            used_a, used_b = [0] * 4, [0] * 4
            delta = [0] * 4
            cost = 0
            for (x, y), take in zip(arcs, choices):
                if take == 0:
                    continue
                if take == 2 and (na[x] < 2 or nb[y] < 2):
                    break
                used_a[x] += take
                used_b[y] += take
                if used_a[x] > na[x] or used_b[y] > nb[y]:
                    break
                delta[x] -= take
                delta[y] += take
                cost += take
            else:
                key = (delta[2] + delta[3], delta[1] + delta[3])
                if key not in out or cost < out[key]:
                    out[key] = cost
        return out

    for case in range(60):
        na = [rng.randrange(0, 3) for _ in range(4)]
        nb = [rng.randrange(0, 3) for _ in range(4)]
        expect = matchings(na, nb)
        got = {key: cost for key, (cost, _delta) in _cell_moves(na, nb).items()}
        assert got == expect, f'第 {case} 例格子刻画不一致：{got} != {expect}'
    check('60 组格子净变化刻画与匹配枚举一致', True)

    def brute_cells(cells, origin, set_p, set_c):
        per_cell = []
        for na, nb in cells:
            per_cell.append(list(matchings(na, nb).items()))
        best = None
        for combo in product(*per_cell):
            p = origin[0] + sum(move[0][0] for move in combo)
            c = origin[1] + sum(move[0][1] for move in combo)
            if p in set_p and c in set_c:
                cost = sum(move[1] for move in combo)
                if best is None or cost < best:
                    best = cost
        return best

    class FakeCounts:
        def __init__(self, mature, picked_up, complained):
            self.mature = mature
            self.picked_up = picked_up
            self.complained = complained

    class FakeCtx:
        pass

    solved = 0
    for case in range(80):
        n_cells = rng.randrange(1, 4)
        raw = [([rng.randrange(0, 3) for _ in range(4)], [rng.randrange(0, 3) for _ in range(4)])
               for _ in range(n_cells)]
        cells = [{'A': [tuple(f'A{i}-{t}-{k}' for k in range(na[t])) for t in range(4)],
                  'B': [tuple(f'B{i}-{t}-{k}' for k in range(nb[t])) for t in range(4)]}
                 for i, (na, nb) in enumerate(raw)]
        ctx = FakeCtx()
        ctx.a = FakeCounts(40, 20, 8)
        ctx.b = FakeCounts(40, 20, 8)
        from src.domain.swap import _reach
        reach_p = _reach(ctx, cells, 'p')
        reach_c = _reach(ctx, cells, 'c')
        pool_p = list(range(reach_p[0], reach_p[1] + 1))
        pool_c = list(range(reach_c[0], reach_c[1] + 1))
        set_p = set(rng.sample(pool_p, min(len(pool_p), rng.randrange(1, 4))))
        set_c = set(rng.sample(pool_c, min(len(pool_c), rng.randrange(1, 4))))
        expect = brute_cells(raw, (20, 8), set_p, set_c)
        found = _search(cells, (20, 8), (set_p, set_c), (reach_p, reach_c), None)
        got = found[0] if found else None
        assert got == expect, f'第 {case} 例：搜索 {got}，暴力 {expect}'
        if expect is not None:
            capped = _search(cells, (20, 8), (set_p, set_c), (reach_p, reach_c), expect)
            assert capped is None or capped[0] < expect, '剪枝上界处理有误'
            solved += 1
    check('80 组二维搜索与暴力一致', solved > 0)


def test_greedy_failure_needs_search():
    print('贪心失效而搜索可解的情形')
    cells = [
        {'A': [(), ('A1',), (), ()], 'B': [(), (), ('B1',), ()]},  # (+1,-1)
        {'A': [('A2',), (), (), ()], 'B': [(), (), (), ('B2',)]},  # (+1,+1)
    ]
    # 需要 A 组取件 +2、投诉 +0：只能 (+1,-1)+(+1,+1)，贪心轴向不可得
    greedy = _greedy(cells, 2, 0)
    check('贪心确实无解', greedy is None)
    found = _search(cells, (0, 0), ({2}, {0}), ((0, 2), (0, 1)), None)
    check('搜索找到 2 对互换', found is not None and found[0] == 2, str(found))


def test_scope_and_validation():
    print('圈定范围、未成熟单与未知单号')
    rows = demo_rows()
    suspects = ['LW-1118-0001', 'LW-1118-0002', 'LW-1118-1221', 'LW-1118-1245', 'LW-1118-1201']
    result = review_swaps(rows, {}, suspects)
    reasons = {item['assignment_id']: item['reason'] for item in result['excluded']}
    check('截止日及之后分配被剔除', 'LW-1118-1221' in reasons and '截止日' in reasons['LW-1118-1221'])
    check('未成熟分配被剔除', 'LW-1118-1201' in reasons and '未成熟' in reasons['LW-1118-1201'])
    check('成熟存疑单计数正确', result['mature_count'] == result['suspect_count'] - len(result['excluded']))
    try:
        review_swaps(rows, {}, ['LW-1118-0001', 'NOPE-1'])
        check('未知单号报错', False)
    except ValueError as error:
        check('未知单号报错', 'NOPE-1' in str(error))
    try:
        normalize_suspects([str(i) for i in range(1001)])
        check('超过圈定上限报错', False)
    except ValueError:
        check('超过圈定上限报错', True)
    try:
        normalize_suspects([])
        check('空圈定报错', False)
    except ValueError:
        check('空圈定报错', True)


def test_edge_scopes():
    print('边界圈定：全部未成熟、单张成熟、重复与空白单号')
    rows = demo_rows()
    result = review_swaps(rows, {}, ['LW-1118-1201', 'LW-1118-1225', 'LW-1118-1221'])
    check('全部未成熟或超截止时无法翻转', result['state'] == 'impossible')
    check('成熟可互换为零', result['mature_count'] == 0)
    result = review_swaps(rows, {}, ['LW-1118-0001'])
    check('单张成熟存疑单无法配对', result['state'] == 'impossible' and result['mature_count'] == 1)
    cleaned = normalize_suspects([' LW-1118-0001 ', 'LW-1118-0001', 'LW-1118-0002'])
    check('重复与空白归一', cleaned == ['LW-1118-0001', 'LW-1118-0002'], str(cleaned))


def test_consistency_and_backfill():
    print('迟到记录补齐后按原圈定范围重算；旧数据重跑结果一致')
    rows = demo_rows()
    rng = Random(1182026)
    suspects = rng.sample([row['assignment_id'] for row in rows[:1200]], 200)
    first = review_swaps(rows, {}, suspects)
    again = review_swaps(rows, {}, suspects)
    check('同一数据重跑结果一致', first == again)
    from datetime import date, timedelta
    backfilled = [dict(row) for row in rows]
    changed = 0
    for row in backfilled[:1200]:
        if row['picked_up_on'] is None and changed < 40:
            assigned = date.fromisoformat(row['assigned_on'])
            row['picked_up_on'] = (assigned + timedelta(days=1)).isoformat()
            changed += 1
    recomputed = review_swaps(backfilled, {}, suspects)
    check('补齐后按原圈定范围给出确切结论', recomputed['state'] in ('flipped', 'impossible'))
    check('旧数据结论不受补齐影响', review_swaps(rows, {}, suspects) == first)
    print(f'    补齐前：{first["headline"]}；补齐后：{recomputed["headline"]}')


def test_search_performance():
    print('二维搜索性能：200 张存疑单、21 个格子')
    rng = Random(2611)
    cells = []
    for i in range(21):
        cells.append({
            'A': [tuple(f'A{i}-{t}-{k}' for k in range(rng.randrange(0, 4))) for t in range(4)],
            'B': [tuple(f'B{i}-{t}-{k}' for k in range(rng.randrange(0, 4))) for t in range(4)],
        })
    total = sum(len(cell['A'][t]) + len(cell['B'][t]) for cell in cells for t in range(4))
    class FakeCounts:
        def __init__(self, mature, picked_up, complained):
            self.mature = mature
            self.picked_up = picked_up
            self.complained = complained

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.a = FakeCounts(100, 60, 10)
    ctx.b = FakeCounts(100, 60, 10)
    from src.domain.swap import _reach
    reach_p = _reach(ctx, cells, 'p')
    reach_c = _reach(ctx, cells, 'c')
    set_p = set(range(reach_p[0], reach_p[0] + 5))
    set_c = set(range(reach_c[0], reach_c[0] + 5))
    started = time.perf_counter()
    _search(cells, (60, 10), (set_p, set_c), (reach_p, reach_c), None)
    elapsed = time.perf_counter() - started
    print(f'    {total} 张存疑单，{elapsed:.3f}s')
    check('二维搜索在可接受时间内完成', elapsed < 30)


def main():
    test_baseline()
    test_demo_flip_out()
    test_demo_flip_in()
    test_brute_force_small()
    test_brute_force_random_params()
    test_cell_moves_and_search()
    test_greedy_failure_needs_search()
    test_scope_and_validation()
    test_edge_scopes()
    test_consistency_and_backfill()
    test_search_performance()
    if FAILURES:
        print(f'\n失败 {len(FAILURES)} 项：{FAILURES}')
        sys.exit(1)
    print('\n全部通过')


if __name__ == '__main__':
    main()
