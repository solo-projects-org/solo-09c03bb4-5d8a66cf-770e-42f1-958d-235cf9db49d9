#!/usr/bin/env python3
"""核查演示：共享备选不足的批次，怎么用只读核查接口复核。

用法: python scripts/audit_demo.py [base_url]   (默认 http://127.0.0.1:8000)

演示步骤：
1. 登记一轮清册：3 个专题共 8 个名额，共享 4 张备选（C1~C4）。
2. 提交冲红批 P1，一次冲掉全部 8 张样本 -> 只能补 4 个，回执剩余缺额 4。
3. 查 P1 核查结果：缺额组、各专题补前缺额、组内全部备选票号（共享只列一次），
   并核验“缺额合计 - 备选票数 == 回执剩余缺额”。
4. 后续再提交冲红批 P2（冲掉 P1 的替补票与剩余备选）。
5. 重新核查 P1：结果与第 3 步逐字节一致（历史回放，不读现行名单）。
"""
import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RID = f"AUDIT-{int(time.time())}"


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    c = httpx.Client(base_url=BASE, timeout=30)

    roster = {
        "round_id": RID,
        "topics": [
            {"topic_code": "价税异常", "quota": 3, "samples": ["A1", "A2", "A3"],
             "candidates": ["C1", "C2", "C3"]},
            {"topic_code": "进销项背离", "quota": 3, "samples": ["B1", "B2", "B3"],
             "candidates": ["C2", "C3", "C4"]},
            {"topic_code": "顶额开具", "quota": 2, "samples": ["D1", "D2"],
             "candidates": ["C1", "C4"]},
        ],
    }
    r = c.post("/rounds", json=roster)
    r.raise_for_status()
    show(f"1. 登记清册 {RID}：8 个名额，仅 4 张共享备选 C1~C4", r.json())

    r = c.post(f"/rounds/{RID}/redflush-batches",
               json={"batch_no": "P1",
                     "invoice_nos": ["A1", "A2", "A3", "B1", "B2", "B3", "D1", "D2"]})
    r.raise_for_status()
    receipt = r.json()
    show("2. 冲红批 P1：8 张样本全退，回执只能补 4 个", {
        "topics": receipt["topics"],
        "total_filled": receipt["total_filled"],
        "round_shortfall": sum(t["shortfall"] for t in receipt["topics"]),
    })

    first_audit = c.get(f"/rounds/{RID}/redflush-batches/P1/audit")
    first_audit.raise_for_status()
    show("3. 核查 P1（只读）：共享备选不足的专题组", first_audit.json())
    chk = first_audit.json()["check"]
    assert chk["matches"], chk
    assert chk["difference"] == chk["round_shortfall"], chk
    print(f"\n   核验通过：{chk['vacancy_before_total']} - {chk['candidate_count']}"
          f" = {chk['difference']}（回执剩余缺额 {chk['round_shortfall']}）")

    # 后续冲红：冲掉 P1 的替补票与剩余备选
    later = ["C1", "C2", "C3", "C4"]
    r = c.post(f"/rounds/{RID}/redflush-batches",
               json={"batch_no": "P2", "invoice_nos": later})
    r.raise_for_status()
    show("4. 后续冲红批 P2：冲掉 P1 补上的票与剩余备选", {
        "topics": r.json()["topics"], "total_filled": r.json()["total_filled"],
    })

    second_audit = c.get(f"/rounds/{RID}/redflush-batches/P1/audit")
    second_audit.raise_for_status()
    assert second_audit.json() == first_audit.json(), "后续冲红改变了核查结果"
    show("5. 再次核查 P1：与第 3 步完全一致（后续冲红不影响）", second_audit.json())

    print("\n演示完成：第 3 步等式成立；第 4 步冲红后 P1 核查结果不变。")


if __name__ == "__main__":
    main()
