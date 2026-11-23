const body = document.body;
const statusBox = document.querySelector('#publish-status');
const result = document.querySelector('#cb-result');
const sid = body.dataset.settlementId;

function setMessage(text, kind) {
  result.textContent = text;
  result.className = kind || '';
}

function paintStatus(data) {
  statusBox.querySelector('b').textContent = data.status;
  statusBox.dataset.revision = data.revision;
  statusBox.classList.toggle('is-published', data.status === '已发布');
  statusBox.classList.toggle('is-failed', String(data.status).startsWith('发布失败'));
  const small = statusBox.querySelector('small');
  if (small) { small.remove(); }
  if (data.published_at) {
    const tag = document.createElement('small');
    tag.textContent = '发布于 ' + data.published_at;
    statusBox.appendChild(tag);
  }
}

async function call(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) { throw Object.assign(new Error(data.detail || '请求失败'), { status: response.status, code: data.code }); }
  return data;
}

document.querySelector('#rerun-btn').addEventListener('click', async (event) => {
  event.target.disabled = true;
  setMessage('按不可变输入快照重算中…');
  try {
    const data = await call('/api/cobrand/settlements/' + sid + '/rerun', { method: 'POST' });
    setMessage('重算完成：结算号 ' + data.manifest.settlement_id + '（与规则版本一致则同号，预算未重复占用）', 'ok');
    paintStatus(data);
  } catch (e) {
    setMessage(e.message, 'err');
  } finally {
    event.target.disabled = false;
  }
});

document.querySelector('#publish-btn').addEventListener('click', async (event) => {
  event.target.disabled = true;
  setMessage('发布前重算并核对原始出资依据…');
  try {
    // 乐观锁：提交当前 revision；陈旧提交 409。发布失败时服务端保留旧版。
    const data = await call('/api/cobrand/settlements/' + sid + '/publish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: Number(statusBox.dataset.revision) })
    });
    paintStatus(data);
    setMessage('发布成功，当前生效版本 revision=' + data.revision, 'ok');
  } catch (e) {
    setMessage('发布中止：' + e.message + '（旧发布版保持生效）', 'err');
    if (e.status === 409) {
      call('/api/cobrand/settlements/' + sid).then(paintStatus).catch(() => {});
    }
  } finally {
    event.target.disabled = false;
  }
});

// 初次渲染状态样式。
paintStatus({ status: statusBox.querySelector('b').textContent, revision: Number(statusBox.dataset.revision), published_at: null });
