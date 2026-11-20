#!/usr/bin/env python3
"""现场试跑：共享备选 + 并发冲红 + 幂等重送/冲突 + 现行名单与回执查询。

用法: python scripts/demo.py [base_url]   (默认 http://127.0.0.1:8000)
"""
import concurrent.futures as cf
import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RID = f"DEMO-{int(time.time())}"


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    c = httpx.Client(base_url=BASE, timeout=30)

    roster = {
        "round_id": RID,
        "topics": [
            {"topic_code": "价税异常", "quota": 2, "samples": ["A1", "A2"],
             "candidates": ["C1", "C2", "C3"]},
            {"topic_code": "进销项背离", "quota": 2, "samples": ["B1", "B2"],
             "candidates": ["C2", "C3", "C4"]},
            {"topic_code": "顶额开具", "quota": 1, "samples": ["D1"],
             "candidates": ["C1", "C4"]},
        ],
    }
    r = c.post("/rounds", json=roster)
    r.raise_for_status()
    show(f"1. 登记清册 {RID}（备选 C1~C4 跨专题共享）", r.json())

    r = c.post(f"/rounds/{RID}/redflush-batches",
               json={"batch_no": "P1", "invoice_nos": ["A1", "B1"]})
    show("2. 冲红批 P1（A1、B1 退出样本，按最大匹配补样）", r.json())

    batches = {
        "P2": ["A2"], "P3": ["B2"], "P4": ["D1"], "P5": ["C3"],
        "P6": ["C4"], "P7": ["ZZZ-未知票"], "P8": ["A1"], "P9": ["C1"],
    }

    def submit(kv):
        batch_no, invs = kv
        resp = c.post(f"/rounds/{RID}/redflush-batches",
                      json={"batch_no": batch_no, "invoice_nos": invs})
        return resp.status_code, resp.json()

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(submit, batches.items()))
    assert all(sc == 201 for sc, _ in results), results
    receipts = c.get(f"/rounds/{RID}/redflush-batches").json()
    show(f"3. 并发提交 {len(batches)} 个冲红批后的全部回执（按生效顺序 seq）", receipts)

    state = c.get(f"/rounds/{RID}").json()
    show("4. 现行名单（样本/备选/缺额）", state)

    replay = c.post(f"/rounds/{RID}/redflush-batches",
                    json={"batch_no": "P1", "invoice_nos": ["B1", "A1"]})
    first = c.get(f"/rounds/{RID}/redflush-batches/P1").json()
    assert replay.status_code == 200 and replay.json() == first
    show("5. 同轮同批号重送同组票号（换序）-> 200 原回执", replay.json())

    conflict = c.post(f"/rounds/{RID}/redflush-batches",
                      json={"batch_no": "P1", "invoice_nos": ["A2"]})
    show(f"6. 同轮同批号换一组票号 -> HTTP {conflict.status_code}", conflict.json())

    print("\n试跑完成：以上 5、6 的断言均已通过。")


if __name__ == "__main__":
    main()
