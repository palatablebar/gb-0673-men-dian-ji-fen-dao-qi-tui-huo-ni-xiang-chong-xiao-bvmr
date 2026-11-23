"""联名积分与出资结算引擎。

设计选择：退款严格沿用原消费的出资构成（frozen funding composition）。
每笔消费在落账瞬间把品牌方/商场方各出资多少冻结为不可变依据；
退款按该构成等比拆分（最大余数法：零头按余数大小分配，余数相同再按 brand→mall 顺序，
保证整数且合计精确），同步 (1) 恢复会员带出资标签的可用积分，(2) 释放对应出资池预算。
绝不由当前活动池承接——否则追溯链断裂且可能透支非原出资方预算。
"""
import base64
import hashlib
import json
import time
from copy import deepcopy
from datetime import datetime, timezone

from app.coerce import instant
from app.errors import BusinessError
from app.cobrand.constants import RULE_VERSION, POOLS, PRIORITY, MAX_BYTES
from app.cobrand.reader import parse_input


def largest_remainder(amount, ratio):
    """按 ratio 比例拆分正整数 amount。零头按余数大小、再按 POOLS 顺序归属。"""
    total = sum(ratio.values())
    raw = {p: amount * ratio[p] / total for p in POOLS}
    floors = {p: int(raw[p] // 1) for p in POOLS}
    rest = amount - sum(floors.values())
    order = sorted(POOLS, key=lambda p: (-(raw[p] - floors[p]), POOLS.index(p)))
    for p in order[:rest]:
        floors[p] += 1
    assert sum(floors.values()) == amount
    return floors


def evidence_hash(event):
    """原始事件依据的防篡改指纹。"""
    return hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _dry_split(member_buckets, member, points):
    """预演消费拆分（只读），返回 {pool: 数量} 或 None（无账户/余额不足）。"""
    if member not in member_buckets:
        return None
    avail = {p: member_buckets[member][p] for p in POOLS}
    if sum(avail.values()) < points:
        return None
    need = dict(points=points)
    plan = {}
    for p in POOLS:
        take = min(avail[p], need['points'])
        plan[p] = take
        need['points'] -= take
    return plan if need['points'] == 0 else None


def replay(parsed):
    events = sorted(parsed['events'], key=lambda e: (instant(e['at']), PRIORITY[e['type']], e['id']))
    quarantine = list(parsed['quarantine'])
    budgets = {p: 0 for p in POOLS}
    issued = {p: 0 for p in POOLS}           # 累计发放（按出资池）
    gross = {p: 0 for p in POOLS}          # 累计支出（消费占用）
    released = {p: 0 for p in POOLS}       # 累计退款释放
    buckets = {}                           # 会员 -> 池 -> 当前带标签可用积分
    spends = {}                            # 消费号 -> 不可变出资依据
    member_restored = {}                   # 会员 -> 退款当天恢复额
    spend_rows, refund_rows = [], []

    def quarantine_of(event, code, detail):
        quarantine.append({'id': event.get('id', ''), 'code': code, 'detail': detail, 'raw': event})

    for event in events:
        etype, eid, points = event['type'], event['id'], event.get('points')
        if etype == 'BUDGET':
            budgets[event['pool']] = event['budget']
            continue
        member = event['member']
        if etype == 'ISSUE':
            buckets.setdefault(member, {p: 0 for p in POOLS})
            member_restored.setdefault(member, 0)
            for p in POOLS:
                buckets[member][p] += event['shares'][p]
                issued[p] += event['shares'][p]
            continue
        member_restored.setdefault(member, 0)
        if etype == 'SPEND':
            if member not in buckets:
                quarantine_of(event, 'UNKNOWN_MEMBER', '会员无联名积分发放记录，消费整笔失败')
                continue
            plan = _dry_split(buckets, member, points)
            if plan is None:
                quarantine_of(event, 'INSUFFICIENT_POINTS', '会员可用积分不足，消费整笔失败、不动任何余额')
                continue
            # 预算透支预检：任何一方净支出超预算则整笔隔离，双方预算均不被占用。
            over = next((p for p in POOLS if gross[p] + plan[p] - released[p] > budgets[p]), None)
            if over is not None:
                quarantine_of(event, 'BUDGET_OVERDRAFT', f'{over} 池预算将透支，消费整笔失败、双方预算均不占用')
                continue
            composition = []
            for p in POOLS:
                buckets[member][p] -= plan[p]
                gross[p] += plan[p]
                composition.append({'pool': p, 'points': plan[p]})
            spends[eid] = {'id': eid, 'member': member, 'points': points,
                           'funding': composition, 'refunded': 0, 'at': event['at'],
                           'evidence': evidence_hash(event)}
            spend_rows.append({'id': eid, 'member': member, 'points': points,
                               'brand_share': plan['brand'], 'mall_share': plan['mall'],
                               'evidence': spends[eid]['evidence'], 'at': event['at']})
            continue
        # REFUND
        if member not in buckets:
            quarantine_of(event, 'UNKNOWN_MEMBER', '会员无联名积分发放记录，事件整笔隔离')
            continue
        original = spends.get(event['ref'])
        if original is None or original['member'] != member:
            quarantine_of(event, 'REFUND_REFERENCE', '退款未关联同一会员的有效消费')
            continue
        if points > original['points'] - original['refunded']:
            quarantine_of(event, 'REFUND_CAP', '累计退款超过原消费可退额度')
            continue
        ratio = {x['pool']: x['points'] for x in original['funding']}
        split = largest_remainder(points, ratio)
        # 按原出资构成释放，不得超过该池在本笔消费中的出资额（最大余数法下恒成立）。
        for p in POOLS:
            assert split[p] <= ratio[p]
        # 1) 释放出资池预算（按原出资构成，不超过该池累计支出，结构性不透支）。
        for p in POOLS:
            released[p] += split[p]
        # 2) 恢复会员带标签可用额度：沿该会员的消费条目逆向、按池回补。
        restore = dict(split)
        for sp in reversed(list(spends.values())):
            if sp['member'] != member:
                continue
            for item in sp['funding']:
                p = item['pool']
                if restore[p] <= 0:
                    continue
                back = min(restore[p], item['points'])
                buckets[member][p] += back
                restore[p] -= back
            if all(v == 0 for v in restore.values()):
                break
        assert all(v == 0 for v in restore.values())
        original['refunded'] += points
        member_restored[member] += points
        refund_rows.append({'id': eid, 'member': member, 'ref': event['ref'], 'points': points,
                            'restored': points, 'brand_release': split['brand'],
                            'mall_release': split['mall'], 'basis': original['evidence'], 'at': event['at']})

    pools = []
    for p in POOLS:
        net = gross[p] - released[p]
        pools.append({'pool': p, 'budget': budgets[p], 'issued': issued[p],
                      'gross_spend': gross[p], 'released': released[p],
                      'net_spend': net, 'remaining': budgets[p] - net})
    members = [{'member': m,
                'available': sum(buckets[m].values()),
                'restored': member_restored.get(m, 0),
                'brand_balance': buckets[m]['brand'],
                'mall_balance': buckets[m]['mall']}
               for m in sorted(buckets)]
    return {'pools': pools, 'members': members, 'spends': spend_rows, 'refunds': refund_rows,
            'quarantine': quarantine, 'spend_evidence': spends, 'buckets': buckets}


def _invariants(ledger):
    pools = {x['pool']: x for x in ledger['pools']}
    members = ledger['members']
    member_pool_balance = {p: sum(m[f'{p}_balance'] for m in members) for p in POOLS}
    released_total = sum(x['released'] for x in ledger['pools'])
    restored_total = sum(m['restored'] for m in members)
    refund_points = sum(r['points'] for r in ledger['refunds'])
    checks = {
        # 守恒一：每池净支出 = 消费占用合计 − 退款释放合计
        'pool_conservation': all(
            pools[p]['net_spend']
            == sum(s['brand_share' if p == 'brand' else 'mall_share'] for s in ledger['spends'])
            - sum(r['brand_release' if p == 'brand' else 'mall_release'] for r in ledger['refunds'])
            for p in POOLS),
        # 守恒二：双方预算均不透支
        'no_overdraft': all(x['remaining'] >= 0 for x in ledger['pools']),
        # 守恒三：退款释放合计 = 会员恢复合计 = 退款积分合计（当天恢复、逐笔可追溯）
        'refund_restoration': released_total == restored_total == refund_points,
        # 守恒四：退款逐笔与原消费出资构成对得上
        'funding_basis': all(
            r['brand_release'] + r['mall_release'] == r['points']
            and r['basis'] in {s['evidence'] for s in ledger['spends']}
            for r in ledger['refunds']),
        # 守恒五：按池严格守恒——发放 = 会员带标签可用 + 净支出
        'member_balance': all(
            pools[p]['issued'] == member_pool_balance[p] + pools[p]['net_spend'] for p in POOLS),
    }
    checks['balanced'] = all(checks.values())
    return checks


def evaluate(payload=None, snapshot=None):
    payload = payload or {}
    if snapshot is not None:
        raw = base64.b64decode(snapshot['bytes_b64'], validate=True)
        partners = deepcopy(snapshot['partners'])
    elif 'json_text' in payload:
        raw = str(payload['json_text']).encode('utf-8')
        partners = None
    else:
        from app.config import ROOT
        raw = (ROOT / 'sample_data' / 'cobrand_demo.json').read_bytes()
        partners = None
    if len(raw) > MAX_BYTES:
        raise BusinessError('TOO_LARGE', '文件超过2MB', 413)
    parsed = parse_input(raw)
    if partners is not None:
        parsed['partners'] = partners
    start = time.perf_counter()
    ledger = replay(parsed)
    checks = _invariants(ledger)
    digest_src = hashlib.sha256(raw + RULE_VERSION.encode()).hexdigest()
    settlement_id = 'cb-' + digest_src[:16]
    ledger['manifest'] = {
        'settlement_id': settlement_id,
        'sha256': digest_src,
        'rule_version': RULE_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'elapsed_ms': round((time.perf_counter() - start) * 1000, 3),
        'input_events': len(parsed['events']),
        'quarantined_events': len(ledger['quarantine']),
    }
    ledger['partners'] = parsed['partners']
    ledger['checks'] = checks
    ledger['input_snapshot'] = {'bytes_b64': base64.b64encode(raw).decode(),
                                'partners': deepcopy(parsed['partners'])}
    return ledger
