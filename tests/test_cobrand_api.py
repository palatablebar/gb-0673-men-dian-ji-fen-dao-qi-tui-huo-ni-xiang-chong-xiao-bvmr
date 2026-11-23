import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import main
from app.cobrand import store
from app.cobrand.engine import evaluate


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / 'cobrand.sqlite')
    monkeypatch.setattr(main.app.state, 'cobrand_db', db)
    return TestClient(main.app)


def test_page_renders_only_allowed_sections(client):
    html = client.get('/cobrand').text
    assert '联名积分与出资结算' in html
    assert '会员恢复额' in html
    assert '预算结余' in html
    assert '发布' in html
    # 页面不展开其他营销活动：无消费/退款明细等内部账表字段
    assert '出资构成' not in html and '隔离' not in html


def test_settlement_idempotent_same_id(client):
    a = client.post('/api/cobrand/settlements', json={}).json()
    b = client.post('/api/cobrand/settlements', json={}).json()
    assert a['manifest']['settlement_id'] == b['manifest']['settlement_id']
    history = client.get('/api/cobrand/settlements').json()
    assert len(history) == 1


def test_rerun_keeps_id_and_no_extra_budget(client):
    first = client.post('/api/cobrand/settlements', json={}).json()
    sid = first['manifest']['settlement_id']
    rerun = client.post(f'/api/cobrand/settlements/{sid}/rerun').json()
    assert rerun['manifest']['settlement_id'] == sid
    assert rerun['pools'] == first['pools']
    assert len(client.get('/api/cobrand/settlements').json()) == 1


def test_publish_success_then_active_and_archived(client):
    s = client.post('/api/cobrand/settlements', json={}).json()
    sid = s['manifest']['settlement_id']
    pub = client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 1}).json()
    assert pub['status'] == '已发布' and pub['revision'] == 2
    assert client.get('/api/cobrand/active').json()['settlement_id'] == sid
    # 页面默认加载已发布版本
    assert client.get('/cobrand').text.count(sid) >= 1


def test_stale_publish_409_keeps_old_version(client):
    s = client.post('/api/cobrand/settlements', json={}).json()
    sid = s['manifest']['settlement_id']
    client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 1})
    stale = client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 1})
    assert stale.status_code == 409 and stale.json()['code'] == 'STALE_VERSION'
    # 当前生效版本仍是第一次发布
    assert client.get('/api/cobrand/active').json()['settlement_id'] == sid


def test_new_publish_archives_previous(client):
    first = client.post('/api/cobrand/settlements', json={}).json()
    client.post(f'/api/cobrand/settlements/{first["manifest"]["settlement_id"]}/publish',
                json={'revision': 1})
    custom = json.dumps({
        'partners': {'brand': '品牌方', 'mall': '商场方'},
        'events': [
            {'id': 'B1', 'type': 'BUDGET', 'pool': 'brand', 'budget': 100, 'at': '2026-11-01T00:00:00+08:00'},
            {'id': 'B2', 'type': 'BUDGET', 'pool': 'mall', 'budget': 100, 'at': '2026-11-01T00:00:00+08:00'},
            {'id': 'I1', 'type': 'ISSUE', 'member': 'M1', 'points': 10,
             'shares': {'brand': 10, 'mall': 0}, 'at': '2026-11-02T09:00:00+08:00'},
        ]})
    second = client.post('/api/cobrand/settlements', json={'json_text': custom}).json()
    client.post(f'/api/cobrand/settlements/{second["manifest"]["settlement_id"]}/publish',
                json={'revision': 1})
    assert client.get('/api/cobrand/active').json()['settlement_id'] == second['manifest']['settlement_id']
    old = client.get(f'/api/cobrand/settlements/{first["manifest"]["settlement_id"]}').json()
    assert old['status'] == '已归档'


def test_tampered_evidence_blocks_publish_and_keeps_old(client, tmp_path):
    db = main.app.state.cobrand_db
    s = client.post('/api/cobrand/settlements', json={}).json()
    sid = s['manifest']['settlement_id']
    # 先发布一个旧版
    client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 1})
    # 直接篡改库内账证（模拟原始出资依据被覆盖）
    import sqlite3
    con = sqlite3.connect(db)
    row = con.execute('SELECT result FROM cobrand_settlements WHERE settlement_id=?', (sid,)).fetchone()
    tampered = json.loads(row[0])
    tampered['pools'][0]['remaining'] += 999
    con.execute('UPDATE cobrand_settlements SET result=? WHERE settlement_id=?',
                (json.dumps(tampered, ensure_ascii=False), sid))
    con.commit()
    con.close()
    # 新结算单作为篡改对象：对同号再次发布，重算核心账证不一致 → 409
    resp = client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 2})
    assert resp.status_code == 409 and resp.json()['code'] == 'EVIDENCE_TAMPERED'
    # 旧发布版仍然是当前生效版本
    assert client.get('/api/cobrand/active').json()['settlement_id'] == sid


def test_export_zip_contains_full_evidence(client):
    s = client.post('/api/cobrand/settlements', json={}).json()
    resp = client.get(f'/api/cobrand/export/{s["manifest"]["settlement_id"]}')
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert {'manifest.json', 'settlement.json', 'input_snapshot.json'} <= names
        payload = json.loads(zf.read('settlement.json'))
        assert payload['checks']['balanced'] is True


def test_missing_settlement_404_and_bad_revision(client):
    assert client.get('/api/cobrand/settlements/nope').status_code == 404
    s = client.post('/api/cobrand/settlements', json={}).json()
    resp = client.post(f'/api/cobrand/settlements/{s["manifest"]["settlement_id"]}/publish',
                       json={'revision': 0})
    assert resp.status_code == 422 and resp.json()['code'] == 'BAD_REVISION'


def test_custom_json_text_roundtrip(client):
    doc = {
        'partners': {'brand': '品牌方', 'mall': '商场方'},
        'events': [
            {'id': 'B1', 'type': 'BUDGET', 'pool': 'brand', 'budget': 100, 'at': '2026-11-01T00:00:00+08:00'},
            {'id': 'B2', 'type': 'BUDGET', 'pool': 'mall', 'budget': 100, 'at': '2026-11-01T00:00:00+08:00'},
            {'id': 'I1', 'type': 'ISSUE', 'member': 'M9', 'points': 10,
             'shares': {'brand': 6, 'mall': 4}, 'at': '2026-11-02T09:00:00+08:00'},
        ]}
    r = client.post('/api/cobrand/settlements', json={'json_text': json.dumps(doc)}).json()
    assert r['members'][0]['member'] == 'M9'
    assert r['checks']['balanced'] is True
    assert store.get(main.app.state.cobrand_db, r['manifest']['settlement_id'])['manifest']['sha256'] == r['manifest']['sha256']


def test_failed_publish_then_rerun_resets_and_republishes(client):
    import sqlite3
    db = main.app.state.cobrand_db
    s = client.post('/api/cobrand/settlements', json={}).json()
    sid = s['manifest']['settlement_id']
    # 陈旧 revision 触发发布失败，旧版（此时无发布版）不受影响
    fail = client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 99})
    assert fail.status_code == 409
    assert str(client.get(f'/api/cobrand/settlements/{sid}').json()['status']).startswith('发布失败')
    # 重算复位为草稿并抬升 revision
    rerun = client.post(f'/api/cobrand/settlements/{sid}/rerun').json()
    assert rerun['status'] == '草稿' and rerun['revision'] == 2
    # 用新 revision 发布成功
    pub = client.post(f'/api/cobrand/settlements/{sid}/publish', json={'revision': 2}).json()
    assert pub['status'] == '已发布' and pub['revision'] == 3
    assert client.get('/api/cobrand/active').json()['settlement_id'] == sid
    # 库内只保留一条结算单，预算未重复占用
    assert len(client.get('/api/cobrand/settlements').json()) == 1
