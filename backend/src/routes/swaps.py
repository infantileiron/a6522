from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role, Status
from src.controllers.auth import allow, current
from src.domain.swap import normalize_suspects, review_swaps
from src.models.identity import User
from src.models.jobs import Run, SwapReview
from src.models.source import Dataset
from src.schemas.requests import SwapRequest
from src.services.audit import record
from src.utils import views
from src.utils.time import now

router = APIRouter()


def require_run(db, run_id):
    item = db.get(Run, run_id)
    if not item:
        raise HTTPException(404, '找不到该运行记录')
    return item


@router.get('/runs/{run_id}/swaps')
def index(run_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    require_run(db, run_id)
    items = db.scalars(select(SwapReview).where(SwapReview.run_id == run_id)
                       .order_by(SwapReview.id.desc()).limit(50))
    return [views.swap(item) for item in items]


@router.post('/runs/{run_id}/swaps', status_code=201)
def create(run_id: int, body: SwapRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    run = require_run(db, run_id)
    if run.status != Status.COMPLETE:
        raise HTTPException(409, '只有计算完成的运行可以复盘组别互换')
    dataset_id = body.dataset_id or run.dataset_id
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到指定的数据批次')
    suspects = normalize_suspects(body.suspects)
    result = review_swaps(dataset.rows, run.params, suspects)
    item = SwapReview(run_id=run.id, dataset_id=dataset_id, suspects=suspects,
                      result=result, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'swap.created', item.id, run_id=run.id,
           dataset_id=dataset_id, state=result['state'], min_swaps=result['min_swaps'])
    db.commit()
    return views.swap(item, detail=True)


@router.get('/swaps/{swap_id}')
def detail(swap_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(SwapReview, swap_id)
    if not item:
        raise HTTPException(404, '找不到该互换复核记录')
    return views.swap(item, detail=True)


@router.post('/swaps/{swap_id}/recompute')
def recompute(swap_id: int, db: Session = Depends(connection),
              actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    item = db.get(SwapReview, swap_id, with_for_update=True)
    if not item:
        raise HTTPException(404, '找不到该互换复核记录')
    run = db.get(Run, item.run_id)
    dataset = db.get(Dataset, item.dataset_id)
    if not run or not dataset:
        raise HTTPException(410, '关联的运行或数据批次已不可用')
    item.result = review_swaps(dataset.rows, run.params, item.suspects)
    item.recomputed_at = now()
    record(db, actor.username, 'swap.recomputed', item.id, run_id=item.run_id,
           dataset_id=item.dataset_id, state=item.result['state'])
    db.commit()
    return views.swap(item, detail=True)
