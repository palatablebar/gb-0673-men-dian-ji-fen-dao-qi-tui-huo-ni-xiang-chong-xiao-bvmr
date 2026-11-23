import json
from app.coerce import instant
from app.errors import BusinessError
from app.cobrand.constants import POOLS, PRIORITY

DEFAULT_PARTNERS = {'brand': '品牌方', 'mall': '商场方'}


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _quarantine(bucket, event, code, detail):
    bucket.append({'id': event.get('id', '') if isinstance(event, dict) else '',
                   'code': code, 'detail': detail, 'raw': event})


def parse_input(raw):
    """解析联名事件 JSON。结构性错误抛 BusinessError(422)，单事件错误进隔离区。"""
    try:
        doc = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BusinessError('BAD_JSON', '事件文件不是有效 UTF-8 JSON')
    if not isinstance(doc, dict) or not isinstance(doc.get('events'), list):
        raise BusinessError('BAD_STRUCTURE', '顶层须为含 events 数组的对象')
    partners = doc.get('partners') or DEFAULT_PARTNERS
    if not isinstance(partners, dict) or not all(isinstance(partners.get(p), str) and partners[p].strip() for p in POOLS):
        raise BusinessError('BAD_PARTNERS', 'partners 须含 brand 与 mall 名称')
    quarantine, events, seen_ids, budget_pools = [], [], set(), set()
    for event in doc['events']:
        if not isinstance(event, dict):
            _quarantine(quarantine, event, 'BAD_RECORD', '事件须为对象')
            continue
        eid = event.get('id')
        if not isinstance(eid, str) or not eid.strip():
            _quarantine(quarantine, event, 'MISSING_ID', '事件编号缺失')
            continue
        # 编号一旦出现即占位：后续同号事件无论内容如何都隔离，绝不进入账本。
        if eid in seen_ids:
            _quarantine(quarantine, event, 'DUPLICATE_ID', '同一事件编号重复提交，整笔隔离不重复占用预算')
            continue
        etype = event.get('type')
        if etype not in PRIORITY:
            _quarantine(quarantine, event, 'BAD_TYPE', '事件类型仅支持 BUDGET/ISSUE/SPEND/REFUND')
            continue
        try:
            instant(event.get('at'))
        except BusinessError:
            _quarantine(quarantine, event, 'BAD_TIME', '事件时刻缺少有效时区偏移')
            continue
        valid = True
        if etype == 'BUDGET':
            pool = event.get('pool')
            if pool not in POOLS:
                _quarantine(quarantine, event, 'BAD_POOL', '出资池只允许 brand 或 mall')
                valid = False
            elif pool in budget_pools:
                _quarantine(quarantine, event, 'DUPLICATE_BUDGET', '同一出资池预算只能设定一次')
                valid = False
            elif not _is_positive_int(event.get('budget')):
                _quarantine(quarantine, event, 'BAD_NUMBER', '预算须为正整数')
                valid = False
            else:
                budget_pools.add(pool)
        else:
            if not _is_positive_int(event.get('points')):
                _quarantine(quarantine, event, 'BAD_NUMBER', '积分须为正整数')
                valid = False
            elif etype == 'ISSUE':
                shares = event.get('shares')
                if not isinstance(shares, dict) or not all(isinstance(shares.get(p), int) and not isinstance(shares.get(p), bool) and shares.get(p) >= 0 for p in POOLS):
                    _quarantine(quarantine, event, 'BAD_SHARE', '发放须给出 brand/mall 非负整数出资额')
                    valid = False
                elif sum(shares[p] for p in POOLS) != event['points']:
                    _quarantine(quarantine, event, 'BAD_SHARE', '双方出资额之和必须等于发放积分')
                    valid = False
            elif etype == 'REFUND':
                ref = event.get('ref')
                if not isinstance(ref, str) or not ref.strip():
                    _quarantine(quarantine, event, 'MISSING_REF', '退款必须引用原消费号')
                    valid = False
        # 只有通过全部校验的事件才占用编号并进入账本，隔离事件不影响后续同号采信。
        if not valid:
            continue
        seen_ids.add(eid)
        events.append(event)
    if not events:
        raise BusinessError('EMPTY_EVENTS', '没有可采信的联名事件')
    return {'partners': partners, 'events': events, 'quarantine': quarantine}
