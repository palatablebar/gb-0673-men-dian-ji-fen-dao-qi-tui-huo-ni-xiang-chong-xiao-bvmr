import io
import json
import zipfile

from app.errors import BusinessError


def serialize(result):
    if not result.get('manifest') or not result.get('checks'):
        raise BusinessError('INCOMPLETE_SETTLEMENT', '结算单尚未完成')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(
            {**result['manifest'], 'revision': result.get('revision', 1),
             'status': result.get('status', '草稿'), 'published_at': result.get('published_at')},
            ensure_ascii=False, indent=2))
        # 财务逐项核对所需的完整证据链：原消费出资冻结依据、退款来源、会员恢复、双方结余。
        archive.writestr('settlement.json', json.dumps(
            {'partners': result['partners'], 'pools': result['pools'], 'members': result['members'],
             'spends': result['spends'], 'refunds': result['refunds'],
             'quarantine': result['quarantine'], 'checks': result['checks']},
            ensure_ascii=False, indent=2))
        archive.writestr('input_snapshot.json', json.dumps(
            result['input_snapshot'], ensure_ascii=False, indent=2))
    return buffer.getvalue()
