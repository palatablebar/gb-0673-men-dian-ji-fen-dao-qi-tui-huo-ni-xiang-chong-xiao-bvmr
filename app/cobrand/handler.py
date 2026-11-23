from fastapi import APIRouter, Body, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.config import ROOT, COBRAND_DB
from app.cobrand import store
from app.cobrand.archive import serialize
from app.cobrand.engine import evaluate
from app.errors import BusinessError

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def _db(request):
    return getattr(request.app.state, 'cobrand_db', None) or COBRAND_DB


def _load(request):
    sid = request.query_params.get('settlement_id')
    path = _db(request)
    if sid:
        result = store.get(path, sid)
    else:
        active_id = store.active(path)
        result = store.get(path, active_id) if active_id else store.save(path, evaluate())
    return result


@router.get('/cobrand')
def page(request: Request):
    result = _load(request)
    return templates.TemplateResponse(request=request, name='cobrand.html',
                                      context={'cb': result})


@router.post('/api/cobrand/settlements')
def create(request: Request, payload: dict = Body(default={})):
    # 同一输入 + 同一规则版本生成同一结算号，重复提交只返回同一条，绝不重复占用预算。
    return store.save(_db(request), evaluate(payload))


@router.get('/api/cobrand/settlements')
def list_settlements(request: Request):
    return store.history(_db(request))


@router.get('/api/cobrand/settlements/{settlement_id}')
def detail(request: Request, settlement_id: str):
    return store.get(_db(request), settlement_id)


@router.post('/api/cobrand/settlements/{settlement_id}/rerun')
def rerun(request: Request, settlement_id: str):
    previous = store.get(_db(request), settlement_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING', '旧结算单缺少源快照，请重新导入')
    # 读取不可变输入快照重新计算；规则未变则结算号不变、预算占用不新增。
    result = evaluate(snapshot=previous['input_snapshot'])
    store.save(_db(request), result)
    return store.reset_draft(_db(request), settlement_id, result)


@router.post('/api/cobrand/settlements/{settlement_id}/publish')
def publish(request: Request, settlement_id: str, payload: dict = Body(...)):
    revision = payload.get('revision')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    path = _db(request)
    stored = store.get(path, settlement_id)
    try:
        recomputed = evaluate(snapshot=stored['input_snapshot'])
    except BusinessError:
        recomputed = None
    return store.publish(path, settlement_id, revision, recomputed)


@router.get('/api/cobrand/active')
def active_settlement(request: Request):
    sid = store.active(_db(request))
    return {'settlement_id': sid, 'result': store.get(_db(request), sid) if sid else None}


@router.get('/api/cobrand/export/{settlement_id}')
def export_settlement(request: Request, settlement_id: str):
    result = store.get(_db(request), settlement_id)
    return Response(serialize(result), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{settlement_id}.zip"'})
