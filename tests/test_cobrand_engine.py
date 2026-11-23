import pytest

from app.cobrand.engine import evaluate, largest_remainder, replay
from app.cobrand.reader import parse_input


@pytest.fixture
def demo():
    return evaluate()


def _doc(events, partners=None):
    return ('{"partners": {"brand": "品牌方", "mall": "商场方"}, "events": '
            + __import__('json').dumps(events, ensure_ascii=False) + '}').encode()


def _budget(events=None, brand=500, mall=500):
    base = [
        {'id': 'B1', 'type': 'BUDGET', 'pool': 'brand', 'budget': brand, 'at': '2026-11-01T00:00:00+08:00'},
        {'id': 'B2', 'type': 'BUDGET', 'pool': 'mall', 'budget': mall, 'at': '2026-11-01T00:00:00+08:00'},
    ]
    return evaluate({'json_text': _doc(base + (events or [])).decode()})


def test_largest_remainder_exact_and_integer():
    # 70/30 退 30 与 60/40 退 10：合计精确、零头按余数归属。
    assert largest_remainder(30, {'brand': 70, 'mall': 30}) == {'brand': 21, 'mall': 9}
    assert largest_remainder(10, {'brand': 60, 'mall': 40}) == {'brand': 6, 'mall': 4}
    for amount in range(1, 50):
        split = largest_remainder(amount, {'brand': 3, 'mall': 7})
        assert split['brand'] + split['mall'] == amount


def test_demo_deterministic_id(demo):
    again = evaluate(snapshot=demo['input_snapshot'])
    assert again['manifest']['settlement_id'] == demo['manifest']['settlement_id']
    assert again['pools'] == demo['pools']


def test_demo_expected_quarantine_codes(demo):
    codes = {q['id']: q['code'] for q in demo['quarantine']}
    assert codes == {
        'BAD4': 'BAD_SHARE', 'B1': 'DUPLICATE_ID', 'R1': 'DUPLICATE_ID',
        'S2': 'BUDGET_OVERDRAFT', 'BAD1': 'INSUFFICIENT_POINTS',
        'BAD2': 'REFUND_CAP', 'BAD3': 'REFUND_REFERENCE',
        'S4': 'BUDGET_OVERDRAFT', 'BAD6': 'UNKNOWN_MEMBER',
    }


def test_demo_pool_remaining(demo):
    pools = {p['pool']: p for p in demo['pools']}
    assert pools['brand']['remaining'] == 436
    assert pools['mall']['remaining'] == 9
    assert all(p['remaining'] >= 0 for p in demo['pools'])


def test_demo_member_restored(demo):
    members = {m['member']: m for m in demo['members']}
    assert members['M001']['restored'] == 30
    assert members['M002']['restored'] == 10
    assert members['M001']['available'] == 60
    assert members['M002']['available'] == 10


def test_refund_follows_original_funding_mix():
    # 混合出资消费 100（60/40），退款 30 必须按原构成拆为 18/12，并恢复同标签额度。
    events = [
        {'id': 'I1', 'type': 'ISSUE', 'member': 'M1', 'points': 100,
         'shares': {'brand': 60, 'mall': 40}, 'at': '2026-11-02T09:00:00+08:00'},
        {'id': 'S1', 'type': 'SPEND', 'member': 'M1', 'points': 100, 'at': '2026-11-03T09:00:00+08:00'},
        {'id': 'R1', 'type': 'REFUND', 'member': 'M1', 'points': 30, 'ref': 'S1',
         'at': '2026-11-04T09:00:00+08:00'},
    ]
    r = _budget(events)
    refund = r['refunds'][0]
    assert (refund['brand_release'], refund['mall_release']) == (18, 12)
    member = r['members'][0]
    assert member['brand_balance'] == 18 and member['mall_balance'] == 12
    assert member['restored'] == 30
    pools = {p['pool']: p for p in r['pools']}
    assert pools['brand']['released'] == 18 and pools['mall']['released'] == 12


def test_duplicate_refund_submitted_twice_never_double_budget():
    events = [
        {'id': 'I1', 'type': 'ISSUE', 'member': 'M1', 'points': 50,
         'shares': {'brand': 50, 'mall': 0}, 'at': '2026-11-02T09:00:00+08:00'},
        {'id': 'S1', 'type': 'SPEND', 'member': 'M1', 'points': 50, 'at': '2026-11-03T09:00:00+08:00'},
        {'id': 'R1', 'type': 'REFUND', 'member': 'M1', 'points': 20, 'ref': 'S1', 'at': '2026-11-04T09:00:00+08:00'},
        {'id': 'R1', 'type': 'REFUND', 'member': 'M1', 'points': 20, 'ref': 'S1', 'at': '2026-11-05T09:00:00+08:00'},
    ]
    r = _budget(events)
    assert len(r['refunds']) == 1
    assert {q['code'] for q in r['quarantine']} == {'DUPLICATE_ID'}
    pools = {p['pool']: p for p in r['pools']}
    assert pools['brand']['released'] == 20  # 重跑同一退款不重复释放/占用预算
    assert r['members'][0]['restored'] == 20


def test_overdraft_spend_quarantined_and_no_pool_touched():
    events = [
        {'id': 'I1', 'type': 'ISSUE', 'member': 'M1', 'points': 30,
         'shares': {'brand': 10, 'mall': 20}, 'at': '2026-11-02T09:00:00+08:00'},
        {'id': 'S1', 'type': 'SPEND', 'member': 'M1', 'points': 30, 'at': '2026-11-03T09:00:00+08:00'},
    ]
    r = _budget(events, brand=500, mall=10)  # 商场净支出 20 > 预算 10
    assert {q['code'] for q in r['quarantine']} == {'BUDGET_OVERDRAFT'}
    assert r['spends'] == []
    assert all(p['gross_spend'] == 0 and p['released'] == 0 for p in r['pools'])
    assert r['members'][0]['available'] == 30  # 失败消费不动会员余额


def test_unknown_member_spend_rejected():
    r = _budget([{'id': 'X1', 'type': 'SPEND', 'member': 'GHOST', 'points': 5,
                  'at': '2026-11-03T09:00:00+08:00'}])
    assert {q['code'] for q in r['quarantine']} == {'UNKNOWN_MEMBER'}
    assert r['spends'] == []


def test_refund_cap_and_reference_rules():
    events = [
        {'id': 'I1', 'type': 'ISSUE', 'member': 'M1', 'points': 50,
         'shares': {'brand': 50, 'mall': 0}, 'at': '2026-11-02T09:00:00+08:00'},
        {'id': 'S1', 'type': 'SPEND', 'member': 'M1', 'points': 50, 'at': '2026-11-03T09:00:00+08:00'},
        {'id': 'R1', 'type': 'REFUND', 'member': 'M1', 'points': 60, 'ref': 'S1', 'at': '2026-11-04T09:00:00+08:00'},
        {'id': 'R2', 'type': 'REFUND', 'member': 'M1', 'points': 5, 'ref': 'NOPE', 'at': '2026-11-04T10:00:00+08:00'},
    ]
    r = _budget(events)
    codes = {q['id']: q['code'] for q in r['quarantine']}
    assert codes['R1'] == 'REFUND_CAP'
    assert codes['R2'] == 'REFUND_REFERENCE'


def test_invariants_balanced_on_demo(demo):
    assert demo['checks']['balanced'] is True
    # 逐笔退款都能追溯到原消费冻结依据
    bases = {s['evidence'] for s in demo['spends']}
    assert all(rf['basis'] in bases for rf in demo['refunds'])


def test_bad_json_and_empty_events():
    from app.errors import BusinessError
    with pytest.raises(BusinessError):
        parse_input(b'{not json')
    with pytest.raises(BusinessError):
        evaluate({'json_text': '{"events": []}'})


def test_per_pool_issued_equation_under_multi_spend_flow():
    events = [
        {'id': 'I1', 'type': 'ISSUE', 'member': 'M1', 'points': 100,
         'shares': {'brand': 60, 'mall': 40}, 'at': '2026-11-02T09:00:00+08:00'},
        {'id': 'I2', 'type': 'ISSUE', 'member': 'M2', 'points': 80,
         'shares': {'brand': 20, 'mall': 60}, 'at': '2026-11-02T10:00:00+08:00'},
        {'id': 'S1', 'type': 'SPEND', 'member': 'M1', 'points': 100, 'at': '2026-11-03T09:00:00+08:00'},
        {'id': 'S2', 'type': 'SPEND', 'member': 'M2', 'points': 30, 'at': '2026-11-03T10:00:00+08:00'},
        {'id': 'R1', 'type': 'REFUND', 'member': 'M1', 'points': 25, 'ref': 'S1', 'at': '2026-11-04T09:00:00+08:00'},
        {'id': 'S3', 'type': 'SPEND', 'member': 'M1', 'points': 15, 'at': '2026-11-05T09:00:00+08:00'},
    ]
    r = _budget(events)
    assert r['checks']['balanced'] is True
    members = {m['member']: m for m in r['members']}
    for pool in r['pools']:
        member_left = sum(m[f"{pool['pool']}_balance"] for m in r['members'])
        # 发放 = 会员带标签可用 + 净支出（按池严格守恒）
        assert pool['issued'] == member_left + pool['net_spend']
        assert pool['gross_spend'] - pool['released'] == pool['net_spend']
    # M1 退款 25 按 60/40 拆为 15/10，随后再消费 15 后余额仍带原标签
    assert members['M1']['restored'] == 25
    refund = r['refunds'][0]
    assert (refund['brand_release'], refund['mall_release']) == (15, 10)
