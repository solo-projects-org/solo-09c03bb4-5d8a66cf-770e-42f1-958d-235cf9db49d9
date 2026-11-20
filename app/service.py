"""业务逻辑：登记清册、处理冲红批、查询现行名单与回执。

所有写函数都必须在 db.immediate_tx() 事务内调用：
查重 -> 整批退出 -> 计算缺额 -> 最大匹配补样 -> 写回执，要么全部生效，要么整体回滚。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .matching import allocate


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon_key(invoices) -> str:
    """票号集合规范化：去重排序，保证同组票号（不论顺序）判定为同一批。"""
    return "\n".join(sorted(set(invoices)))


def _roster_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------- 登记清册

def register_round(conn, payload) -> tuple[dict, bool]:
    """登记一轮清册，返回 (现行名单, 是否新建)。

    同轮次同内容重复登记 -> 返回现行名单（幂等）；内容不同 -> 409。
    """
    round_id = payload.round_id
    digest = _roster_hash(payload.model_dump())

    row = conn.execute("SELECT roster_hash FROM rounds WHERE round_id = ?", (round_id,)).fetchone()
    if row:
        if row["roster_hash"] == digest:
            return get_round_state(conn, round_id), False
        raise ApiError(409, f"轮次 {round_id} 已登记，且清册内容不同")

    # ---- 校验：每张票只能占一个名额 ----
    seen_topics = set()
    sample_owner = {}
    for t in payload.topics:
        if t.topic_code in seen_topics:
            raise ApiError(400, f"专题代码重复: {t.topic_code}")
        seen_topics.add(t.topic_code)
        if len(set(t.samples)) != len(t.samples):
            raise ApiError(400, f"专题 {t.topic_code} 的样本票号重复")
        if len(set(t.candidates)) != len(t.candidates):
            raise ApiError(400, f"专题 {t.topic_code} 的备选票号重复")
        if len(t.samples) > t.quota:
            raise ApiError(400, f"专题 {t.topic_code} 的已发样本数超过名额")
        for inv in t.samples:
            if inv in sample_owner:
                raise ApiError(400, f"票号 {inv} 同时是多个专题的样本")
            sample_owner[inv] = t.topic_code
    for t in payload.topics:
        for inv in t.candidates:
            if inv in sample_owner:
                raise ApiError(400, f"票号 {inv} 既是已发样本又是备选")

    # ---- 写入 ----
    conn.execute(
        "INSERT INTO rounds(round_id, roster_hash, created_at) VALUES (?,?,?)",
        (round_id, digest, _now()),
    )
    for i, t in enumerate(payload.topics):
        conn.execute(
            "INSERT INTO topics(round_id, topic_code, quota, position) VALUES (?,?,?,?)",
            (round_id, t.topic_code, t.quota, i),
        )
        for j, inv in enumerate(t.samples):
            conn.execute(
                "INSERT INTO samples(round_id, topic_code, invoice_no, active, source, position)"
                " VALUES (?,?,?,1,'roster',?)",
                (round_id, t.topic_code, inv, j),
            )
        for j, inv in enumerate(t.candidates):
            conn.execute(
                "INSERT INTO candidates(round_id, topic_code, invoice_no, available, position)"
                " VALUES (?,?,?,1,?)",
                (round_id, t.topic_code, inv, j),
            )
    return get_round_state(conn, round_id), True


# ---------------------------------------------------------------- 冲红批处理

def process_redflush(conn, round_id: str, batch_no: str, invoice_nos) -> tuple[dict, bool]:
    """处理一个冲红批，返回 (回执, 是否重放)。

    同轮同批号：票号集合相同 -> 返回原回执；不同 -> 409。
    """
    if not conn.execute("SELECT 1 FROM rounds WHERE round_id=?", (round_id,)).fetchone():
        raise ApiError(404, f"轮次不存在: {round_id}")

    canon = sorted(set(invoice_nos))
    key = _canon_key(canon)

    row = conn.execute(
        "SELECT invoice_key, receipt_json FROM batches WHERE round_id=? AND batch_no=?",
        (round_id, batch_no),
    ).fetchone()
    if row:
        if row["invoice_key"] == key:
            return json.loads(row["receipt_json"]), True
        raise ApiError(409, f"批号 {batch_no} 在本轮已生效，且票号集合不同")

    topics = conn.execute(
        "SELECT topic_code, quota FROM topics WHERE round_id=? ORDER BY position", (round_id,)
    ).fetchall()
    order = [t["topic_code"] for t in topics]

    # 1) 本批票号整批退出样本名单与备选名单（仍有效的已发样本不受影响）
    removed_samples: dict[str, list[str]] = {}
    removed_cands: dict[str, list[str]] = {}
    known = set()
    for inv in canon:
        for r in conn.execute(
            "SELECT topic_code FROM samples WHERE round_id=? AND invoice_no=? AND active=1",
            (round_id, inv),
        ).fetchall():
            removed_samples.setdefault(r["topic_code"], []).append(inv)
        for r in conn.execute(
            "SELECT topic_code FROM candidates WHERE round_id=? AND invoice_no=? AND available=1",
            (round_id, inv),
        ).fetchall():
            removed_cands.setdefault(r["topic_code"], []).append(inv)
        if conn.execute(
            "SELECT 1 FROM samples WHERE round_id=? AND invoice_no=? "
            "UNION SELECT 1 FROM candidates WHERE round_id=? AND invoice_no=?",
            (round_id, inv, round_id, inv),
        ).fetchone():
            known.add(inv)
        conn.execute("UPDATE samples SET active=0 WHERE round_id=? AND invoice_no=?", (round_id, inv))
        conn.execute("UPDATE candidates SET available=0 WHERE round_id=? AND invoice_no=?", (round_id, inv))

    # 2) 各专题当前缺额与可用备选
    vacancies, cand = {}, {}
    for t in topics:
        tc = t["topic_code"]
        active_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM samples WHERE round_id=? AND topic_code=? AND active=1",
            (round_id, tc),
        ).fetchone()["c"]
        vacancies[tc] = max(0, t["quota"] - active_cnt)
        cand[tc] = [
            r["invoice_no"]
            for r in conn.execute(
                "SELECT invoice_no FROM candidates"
                " WHERE round_id=? AND topic_code=? AND available=1 ORDER BY position",
                (round_id, tc),
            ).fetchall()
        ]

    # 3) 最大匹配补样：每张票只占一个名额，本批补上的名额总数最大
    assign = allocate(order, vacancies, cand)

    # 4) 落库：补入样本名单、消耗掉的备选在所有专题中失效
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS s FROM batches WHERE round_id=?", (round_id,)
    ).fetchone()["s"]
    for tc in order:
        for inv in assign[tc]:
            pos = conn.execute(
                "SELECT COALESCE(MAX(position),-1)+1 AS p FROM samples WHERE round_id=? AND topic_code=?",
                (round_id, tc),
            ).fetchone()["p"]
            conn.execute(
                "INSERT INTO samples(round_id, topic_code, invoice_no, active, source, position)"
                " VALUES (?,?,?,1,?,?)",
                (round_id, tc, inv, f"batch:{batch_no}", pos),
            )
            conn.execute(
                "UPDATE candidates SET available=0 WHERE round_id=? AND invoice_no=?",
                (round_id, inv),
            )

    # 5) 回执：各专题替补票号与剩余缺额
    receipt = {
        "round_id": round_id,
        "batch_no": batch_no,
        "seq": seq,
        "invoice_nos": canon,
        "unknown_invoices": [i for i in canon if i not in known],
        "removed": {
            "samples": {tc: removed_samples[tc] for tc in order if tc in removed_samples},
            "candidates": {tc: removed_cands[tc] for tc in order if tc in removed_cands},
        },
        "topics": [
            {
                "topic_code": tc,
                "vacancy_before": vacancies[tc],
                "replacements": assign[tc],
                "shortfall": vacancies[tc] - len(assign[tc]),
            }
            for tc in order
        ],
        "total_filled": sum(len(v) for v in assign.values()),
        "created_at": _now(),
    }
    conn.execute(
        "INSERT INTO batches(round_id, batch_no, seq, invoice_key, receipt_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (round_id, batch_no, seq, key, json.dumps(receipt, ensure_ascii=False), receipt["created_at"]),
    )
    return receipt, False


# ---------------------------------------------------------------- 查询

def get_round_state(conn, round_id: str) -> dict:
    if not conn.execute("SELECT 1 FROM rounds WHERE round_id=?", (round_id,)).fetchone():
        raise ApiError(404, f"轮次不存在: {round_id}")
    topics = conn.execute(
        "SELECT topic_code, quota FROM topics WHERE round_id=? ORDER BY position", (round_id,)
    ).fetchall()
    out = []
    for t in topics:
        samples = [
            r["invoice_no"]
            for r in conn.execute(
                "SELECT invoice_no FROM samples WHERE round_id=? AND topic_code=? AND active=1"
                " ORDER BY position",
                (round_id, t["topic_code"]),
            ).fetchall()
        ]
        cands = [
            r["invoice_no"]
            for r in conn.execute(
                "SELECT invoice_no FROM candidates WHERE round_id=? AND topic_code=? AND available=1"
                " ORDER BY position",
                (round_id, t["topic_code"]),
            ).fetchall()
        ]
        out.append(
            {
                "topic_code": t["topic_code"],
                "quota": t["quota"],
                "samples": samples,
                "candidates": cands,
                "shortfall": max(0, t["quota"] - len(samples)),
            }
        )
    nb = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE round_id=?", (round_id,)
    ).fetchone()["c"]
    return {"round_id": round_id, "topics": out, "batches_processed": nb}


def get_receipt(conn, round_id: str, batch_no: str) -> dict:
    row = conn.execute(
        "SELECT receipt_json FROM batches WHERE round_id=? AND batch_no=?",
        (round_id, batch_no),
    ).fetchone()
    if not row:
        raise ApiError(404, f"回执不存在: {round_id}/{batch_no}")
    return json.loads(row["receipt_json"])


def list_receipts(conn, round_id: str) -> list[dict]:
    if not conn.execute("SELECT 1 FROM rounds WHERE round_id=?", (round_id,)).fetchone():
        raise ApiError(404, f"轮次不存在: {round_id}")
    return [
        json.loads(r["receipt_json"])
        for r in conn.execute(
            "SELECT receipt_json FROM batches WHERE round_id=? ORDER BY seq", (round_id,)
        ).fetchall()
    ]
