"""API 接线验证：用桩模块替代 fastapi/sqlalchemy/pydantic，导入真实的
src.routes.swaps 并模拟创建、列表、详情、重跑与迟到补齐重算的完整流程。

运行：python3 -I tests/test_api_swaps.py
"""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')


def _install_stubs():
    fastapi = types.ModuleType('fastapi')

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda func: func

        def post(self, *args, **kwargs):
            return lambda func: func

    fastapi.APIRouter = APIRouter
    fastapi.Depends = lambda *args, **kwargs: None
    fastapi.HTTPException = HTTPException
    security = types.ModuleType('fastapi.security')
    security.HTTPBearer = lambda *args, **kwargs: None
    security.HTTPAuthorizationCredentials = object
    fastapi.security = security
    sys.modules['fastapi'] = fastapi
    sys.modules['fastapi.security'] = security

    sqlalchemy = types.ModuleType('sqlalchemy')
    sqlalchemy.create_engine = lambda *args, **kwargs: object()

    class _Query:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

    sqlalchemy.select = lambda *args, **kwargs: _Query()
    sqlalchemy.JSON = object()
    sqlalchemy.String = lambda *args, **kwargs: None
    sqlalchemy.ForeignKey = lambda *args, **kwargs: None
    sqlalchemy.UniqueConstraint = lambda *args, **kwargs: None
    orm = types.ModuleType('sqlalchemy.orm')

    class _Column:
        def __set_name__(self, owner, name):
            self.name = name

        def __get__(self, obj, owner=None):
            if obj is None:
                return self
            return obj.__dict__.get(self.name)

        def __set__(self, obj, value):
            obj.__dict__[self.name] = value

        def desc(self):
            return self

    class _Base:
        def __init__(self, **kwargs):
            for cls in reversed(type(self).__mro__):
                for name in getattr(cls, '__annotations__', {}):
                    self.__dict__.setdefault(name, None)
            self.__dict__.update(kwargs)

    orm.DeclarativeBase = _Base
    orm.Mapped = type('Mapped', (), {'__class_getitem__': classmethod(lambda cls, item: cls)})
    orm.mapped_column = lambda *args, **kwargs: _Column()
    orm.Session = type('Session', (), {})
    orm.sessionmaker = lambda *args, **kwargs: (lambda: None)
    sqlalchemy.orm = orm
    sys.modules['sqlalchemy'] = sqlalchemy
    sys.modules['sqlalchemy.orm'] = orm

    pydantic = types.ModuleType('pydantic')

    class BaseModel:
        model_config = None

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    pydantic.BaseModel = BaseModel
    pydantic.ConfigDict = dict
    pydantic.Field = lambda default=None, **kwargs: default
    sys.modules['pydantic'] = pydantic

    jwt = types.ModuleType('jwt')
    jwt.InvalidTokenError = type('InvalidTokenError', (Exception,), {})
    jwt.encode = lambda *args, **kwargs: 'token'
    jwt.decode = lambda *args, **kwargs: {'sub': 'tester'}
    sys.modules['jwt'] = jwt


_install_stubs()

from src.domain.demo import demo_rows
from src.models.jobs import Run, SwapReview
from src.models.source import Dataset
from src.routes import swaps as swaps_module
from src.schemas.requests import SwapRequest

FAILURES = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok · {name}')
    else:
        FAILURES.append(name)
        print(f'  FAIL · {name} {detail}')


class FakeDb:
    def __init__(self):
        self.store = {}
        self.pending = []
        self.sequence = {}

    def get(self, model, key, **kwargs):
        return self.store.get((model.__name__, key))

    def add(self, obj):
        self.pending.append(obj)

    def flush(self):
        for obj in self.pending:
            if getattr(obj, 'id', None) is None:
                name = type(obj).__name__
                self.sequence[name] = self.sequence.get(name, 0) + 1
                obj.id = self.sequence[name]
            self.store[(type(obj).__name__, obj.id)] = obj
        self.pending.clear()

    def commit(self):
        self.flush()

    def scalars(self, _query):
        return [obj for (name, _), obj in self.store.items() if name == 'SwapReview']


def make_db():
    db = FakeDb()
    rows = demo_rows()
    db.store[('Dataset', 10)] = Dataset(id=10, name='原始批次', rows=rows, row_count=len(rows))
    backfilled = [dict(row) for row in rows]
    from datetime import date, timedelta
    changed = 0
    for row in backfilled[:1200]:
        if row['picked_up_on'] is None and changed < 40:
            assigned = date.fromisoformat(row['assigned_on'])
            row['picked_up_on'] = (assigned + timedelta(days=1)).isoformat()
            changed += 1
    db.store[('Dataset', 11)] = Dataset(id=11, name='补齐批次', rows=backfilled, row_count=len(backfilled))
    db.store[('Run', 1)] = Run(id=1, dataset_id=10, status='complete', params={})
    db.store[('Run', 2)] = Run(id=2, dataset_id=10, status='queued', params={})
    return db


def main():
    actor = types.SimpleNamespace(username='analyst', role='analyst')
    db = make_db()
    from random import Random
    suspects = Random(1182026).sample([row['assignment_id'] for row in demo_rows()[:1200]], 200)

    created = swaps_module.create(1, SwapRequest(suspects=suspects), db, actor)
    check('创建返回确切结论', created['state'] in ('flipped', 'impossible'))
    check('创建返回明细含方案与圈定', 'pairs' in created['result'] and len(created['suspects']) == 200)
    stored = db.get(SwapReview, created['id'])
    check('复核已落库并钉住数据批次', stored is not None and stored.dataset_id == 10)
    print(f'    结论：{created["headline"]}')

    listing = swaps_module.index(1, db, actor)
    check('列表返回该运行的复核', len(listing) == 1 and listing[0]['id'] == created['id'])
    check('列表不含完整方案', 'result' not in listing[0])

    detail = swaps_module.detail(created['id'], db, actor)
    check('详情与创建一致', detail['result'] == created['result'])

    recomputed = swaps_module.recompute(created['id'], db, actor)
    check('旧复盘重跑结果保持一致', recomputed['result'] == created['result'])
    check('重跑记录时间', recomputed['recomputed_at'] is not None)

    follow = swaps_module.create(1, SwapRequest(suspects=list(detail['suspects']), dataset_id=11), db, actor)
    check('迟到补齐后按原圈定范围重算', follow['dataset_id'] == 11 and follow['suspects'] == detail['suspects'])
    check('新批次给出确切结论', follow['state'] in ('flipped', 'impossible'))
    again = swaps_module.recompute(created['id'], db, actor)
    check('旧复核仍用当时的数据', again['result'] == created['result'] and again['dataset_id'] == 10)
    print(f'    补齐前：{created["result"]["headline"]}；补齐后：{follow["result"]["headline"]}')

    try:
        swaps_module.create(9, SwapRequest(suspects=suspects), db, actor)
        check('缺失运行返回 404', False)
    except Exception as error:
        check('缺失运行返回 404', getattr(error, 'status_code', None) == 404)
    try:
        swaps_module.create(2, SwapRequest(suspects=suspects), db, actor)
        check('未完成运行返回 409', False)
    except Exception as error:
        check('未完成运行返回 409', getattr(error, 'status_code', None) == 409)
    try:
        swaps_module.create(1, SwapRequest(suspects=suspects, dataset_id=99), db, actor)
        check('缺失批次返回 404', False)
    except Exception as error:
        check('缺失批次返回 404', getattr(error, 'status_code', None) == 404)
    try:
        swaps_module.create(1, SwapRequest(suspects=['LW-1118-0001', 'NOPE-9']), db, actor)
        check('未知单号返回 422', False)
    except Exception as error:
        check('未知单号返回 422', isinstance(error, ValueError) and 'NOPE-9' in str(error))
    viewer = types.SimpleNamespace(username='viewer', role='viewer')
    check('列表与详情对只读开放', bool(swaps_module.index(1, db, viewer)) and bool(swaps_module.detail(created['id'], db, viewer)))

    if FAILURES:
        print(f'\n失败 {len(FAILURES)} 项：{FAILURES}')
        sys.exit(1)
    print('\n全部通过')


if __name__ == '__main__':
    main()
