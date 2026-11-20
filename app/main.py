"""冲红补样 API（FastAPI 入口，Swagger 见 /docs）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from . import db, service
from .schemas import RedflushIn, RoundIn


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="发票冲红补样 API",
    description=(
        "数仓质检补样：按轮登记各核查专题的名额、已发样本票号与备选票号（备选可跨专题共享）；"
        "按批号提交冲红票号，整批退出样本与备选名单后按最大匹配补样；"
        "同轮同批号重送同组票号返回原回执，换组报冲突；SQLite 持久化，重启可查。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ApiError)
def api_error_handler(_, exc: service.ApiError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health", tags=["meta"], summary="健康检查")
def health():
    return {"status": "ok"}


@app.post("/rounds", tags=["清册"], summary="登记一轮清册（同内容重登幂等）", status_code=201)
def register_round(payload: RoundIn, response: Response):
    with db.immediate_tx() as conn:
        state, created = service.register_round(conn, payload)
    response.status_code = 201 if created else 200
    return state


@app.get("/rounds/{round_id}", tags=["查询"], summary="查询某轮现行名单（样本/备选/缺额）")
def get_round(round_id: str):
    with db.read_conn() as conn:
        return service.get_round_state(conn, round_id)


@app.post(
    "/rounds/{round_id}/redflush-batches",
    tags=["冲红"],
    summary="按批号提交冲红票号（整批退出并补样）",
    status_code=201,
)
def submit_redflush(round_id: str, payload: RedflushIn, response: Response):
    with db.immediate_tx() as conn:
        receipt, replayed = service.process_redflush(
            conn, round_id, payload.batch_no, payload.invoice_nos
        )
    response.status_code = 200 if replayed else 201
    return receipt


@app.get(
    "/rounds/{round_id}/redflush-batches",
    tags=["查询"],
    summary="列出某轮全部冲红回执（按生效顺序）",
)
def list_receipts(round_id: str):
    with db.read_conn() as conn:
        return service.list_receipts(conn, round_id)


@app.get(
    "/rounds/{round_id}/redflush-batches/{batch_no}",
    tags=["查询"],
    summary="查询单个冲红回执",
)
def get_receipt(round_id: str, batch_no: str):
    with db.read_conn() as conn:
        return service.get_receipt(conn, round_id, batch_no)
