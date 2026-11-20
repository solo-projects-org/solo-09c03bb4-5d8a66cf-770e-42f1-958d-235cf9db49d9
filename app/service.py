"""业务逻辑：登记清册、处理冲红批、查询现行名单与回执、历史回执核查。

所有写函数都必须在 db.immediate_tx() 事务内调用：
查重 -> 整批退出 -> 计算缺额 -> 最大匹配补样 -> 写回执，要么全部生效，要么整体回滚。
核查函数是只读的：从历史回执重放当时状态，不读现行名单，因此后续冲红不影响结果。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .matching import allocate, deficiency_group


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


# ---------------------------------------------------------------- 回执核查（只读）

def audit_receipt(conn, round_id: str, batch_no: str) -> dict:
    """核查某张冲红回执：当时还能用的备选票共有多少，缺额能不能补齐。

    核查口径（时间切片在“本批剔除冲红票之后、分配替补之前”）：
    - 从登记清册出发重建备选池，按生效顺序扣掉此前各批（seq 更小）的冲红票号
      与已消耗的替补票号，再扣掉本批的冲红票号；
    - 各专题的缺额直接取回执里的 vacancy_before（本批剔除后、补样前）；
    - 用与补样相同的二部图求“备选不足”的专题组：该组缺额合计减去其备选并集
      （跨专题共享的票只算一次）恰好等于回执的全轮剩余缺额。

    只读取 rounds/topics/candidates/batches 四张表，且只看 seq <= 本批的历史记录，
    不读现行 samples/candidates 状态，因此批号在升级前生效、或之后又发生冲红，
    都不影响核查结果。
    """
    row = conn.execute(
        "SELECT seq, receipt_json FROM batches WHERE round_id=? AND batch_no=?",
        (round_id, batch_no),
    ).fetchone()
    if not row:
        if not conn.execute("SELECT 1 FROM rounds WHERE round_id=?", (round_id,)).fetchone():
            raise ApiError(404, f"轮次不存在: {round_id}")
        raise ApiError(404, f"回执不存在: {round_id}/{batch_no}")

    seq = row["seq"]
    receipt = json.loads(row["receipt_json"])
    order = [
        t["topic_code"]
        for t in conn.execute(
            "SELECT topic_code FROM topics WHERE round_id=? ORDER BY position", (round_id,)
        ).fetchall()
    ]
    vacancies = {t["topic_code"]: t["vacancy_before"] for t in receipt["topics"]}

    # 登记清册里的备选（按登记顺序），作为重建备选池的基线
    roster_cands = {
        tc: [
            r["invoice_no"]
            for r in conn.execute(
                "SELECT invoice_no FROM candidates"
                " WHERE round_id=? AND topic_code=? ORDER BY position",
                (round_id, tc),
            ).fetchall()
        ]
        for tc in order
    }

    # 本批之前已退出备选池的票：此前各批的冲红票号 + 此前各批补掉的替补票号
    unavailable = set(receipt["invoice_nos"])
    for earlier in conn.execute(
        "SELECT receipt_json FROM batches WHERE round_id=? AND seq<? ORDER BY seq",
        (round_id, seq),
    ).fetchall():
        er = json.loads(earlier["receipt_json"])
        unavailable.update(er["invoice_nos"])
        for t in er["topics"]:
            unavailable.update(t["replacements"])

    available = {
        tc: [inv for inv in roster_cands[tc] if inv not in unavailable] for tc in order
    }

    group, unmatched, _max_fillable = deficiency_group(order, vacancies, available)
    round_shortfall = sum(t["shortfall"] for t in receipt["topics"])

    group_cands: list[str] = []
    seen = set()
    for tc in group:
        for inv in available[tc]:
            if inv not in seen:  # 跨专题共享的备选只列一次
                seen.add(inv)
                group_cands.append(inv)
    vacancy_total = sum(vacancies[tc] for tc in group)
    difference = vacancy_total - len(group_cands)

    matches = difference == round_shortfall == unmatched
    if not matches:
        status, message = "inconsistent", "回执与重建的备选池不一致，请核对数据"
    elif round_shortfall == 0:
        status, message = "fully_filled", "已补满"
    else:
        status, message = "shortfall", "备选不足，仍有缺额"

    return {
        "round_id": round_id,
        "batch_no": batch_no,
        "seq": seq,
        "status": status,
        "message": message,
        "round_shortfall": round_shortfall,
        "total_filled": receipt["total_filled"],
        "topic_group": None
        if not group
        else {
            "topics": [
                {"topic_code": tc, "vacancy_before": vacancies[tc]} for tc in group
            ],
            "vacancy_before_total": vacancy_total,
            "candidates": group_cands,
            "candidate_count": len(group_cands),
            "shortfall": difference,
        },
        "check": {
            "vacancy_before_total": vacancy_total,
            "candidate_count": len(group_cands),
            "difference": difference,
            "round_shortfall": round_shortfall,
            "matches": matches,
        },
    }
