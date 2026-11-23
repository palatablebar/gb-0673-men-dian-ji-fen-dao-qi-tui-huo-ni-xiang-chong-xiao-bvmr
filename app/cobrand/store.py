import json
import sqlite3
from pathlib import Path

from app.errors import BusinessError

# 发布校验只比对可重算的核心账证；持久化元数据（revision/status/发布时间）不参与。
_CORE_KEYS = ('pools', 'members', 'spends', 'refunds', 'quarantine', 'checks', 'partners')


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS cobrand_settlements (
            settlement_id TEXT PRIMARY KEY,
            digest TEXT UNIQUE NOT NULL,
            result TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT '草稿',
            published_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cobrand_active (
            slot TEXT PRIMARY KEY DEFAULT 'current',
            settlement_id TEXT NOT NULL
        );
        """
    )
    return con


def save(path, result):
    sid = result['manifest']['settlement_id']
    with connect(path) as con:
        con.execute(
            'INSERT OR IGNORE INTO cobrand_settlements(settlement_id,digest,result) VALUES(?,?,?)',
            (sid, result['manifest']['sha256'], json.dumps(result, ensure_ascii=False)),
        )
    return get(path, sid)


def get(path, settlement_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM cobrand_settlements WHERE settlement_id=?',
                          (settlement_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '结算单不存在', 404)
    result = json.loads(row['result'])
    result.update(revision=row['revision'], status=row['status'], published_at=row['published_at'])
    return result


def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute(
            'SELECT settlement_id,revision,status,published_at FROM cobrand_settlements ORDER BY rowid DESC LIMIT 100')]


def active(path):
    with connect(path) as con:
        row = con.execute('SELECT settlement_id FROM cobrand_active WHERE slot="current"').fetchone()
    return row['settlement_id'] if row else None


def reset_draft(path, settlement_id, result):
    """重算后写回最新结果；若该单曾发布失败则复位为草稿并抬升 revision，便于干净重试。

    已发布/已归档版本不被重算覆盖（INSERT OR IGNORE 已保证同号只存一条，这里仅复位失败态）。
    """
    with connect(path) as con:
        row = con.execute('SELECT status FROM cobrand_settlements WHERE settlement_id=?',
                          (settlement_id,)).fetchone()
        if row and str(row['status']).startswith('发布失败'):
            con.execute('UPDATE cobrand_settlements SET result=?, status=?, revision=revision+1 WHERE settlement_id=?',
                        (json.dumps(result, ensure_ascii=False), '草稿', settlement_id))
    return get(path, settlement_id)


def same_core(stored, recomputed):
    return all(stored.get(k) == recomputed.get(k) for k in _CORE_KEYS)


def _mark_failed(path, settlement_id, reason):
    """独立事务落失败标记：不抬 revision、不动 active，旧发布版继续生效。"""
    with connect(path) as con:
        con.execute('UPDATE cobrand_settlements SET status=? WHERE settlement_id=?',
                    (f'发布失败：{reason}', settlement_id))


def publish(path, settlement_id, revision, recomputed=None):
    """原子发布。任一校验失败：先在独立事务标记失败再抛错，旧版（active 指向）保持不变。"""
    with connect(path) as con:
        row = con.execute('SELECT * FROM cobrand_settlements WHERE settlement_id=?',
                          (settlement_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '结算单不存在', 404)
        stored = json.loads(row['result'])
        if row['revision'] != revision:
            failure = BusinessError('STALE_VERSION', '版本已过期，请刷新后重试', 409)
            reason = '版本已过期'
        elif recomputed is not None and not same_core(stored, recomputed):
            failure = BusinessError('EVIDENCE_TAMPERED',
                                    '原始出资依据已被覆盖且无法恢复，发布中止并保留旧版', 409)
            reason = '账证与重算结果不一致'
        elif not stored.get('checks', {}).get('balanced'):
            failure = BusinessError('UNBALANCED', '结算守恒校验未通过，禁止发布', 422)
            reason = '守恒校验未通过'
        else:
            failure, reason = None, None
            previous = con.execute('SELECT settlement_id FROM cobrand_active WHERE slot="current"').fetchone()
            previous_id = previous['settlement_id'] if previous else None
            if previous_id != settlement_id:
                if previous_id:
                    con.execute('UPDATE cobrand_settlements SET status=? WHERE settlement_id=?',
                                ('已归档', previous_id))
                con.execute('INSERT INTO cobrand_active(settlement_id) VALUES(?) '
                            'ON CONFLICT(slot) DO UPDATE SET settlement_id=excluded.settlement_id',
                            (settlement_id,))
            con.execute('UPDATE cobrand_settlements SET revision=revision+1,status=?,published_at=? '
                        'WHERE settlement_id=?',
                        ('已发布', stored['manifest']['generated_at'], settlement_id))
    if failure is not None:
        _mark_failed(path, settlement_id, reason)  # 独立事务，失败标记不会随异常回滚
        raise failure
    return get(path, settlement_id)
