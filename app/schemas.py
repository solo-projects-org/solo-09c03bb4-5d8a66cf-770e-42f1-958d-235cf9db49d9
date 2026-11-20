"""请求模型与响应模型（Swagger 入参 / 出参）。"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
InvoiceNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class TopicIn(BaseModel):
    topic_code: Code = Field(description="核查专题代码")
    quota: int = Field(ge=0, description="名额")
    samples: List[InvoiceNo] = Field(default_factory=list, description="已下发的样本票号")
    candidates: List[InvoiceNo] = Field(default_factory=list, description="备选票号（可与其他专题共享）")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"topic_code": "价税异常", "quota": 2, "samples": ["A1", "A2"], "candidates": ["C1", "C2"]}
            ]
        }
    }


class RoundIn(BaseModel):
    round_id: Code = Field(description="抽查轮次号")
    topics: List[TopicIn] = Field(min_length=1, description="各核查专题清册")


class RedflushIn(BaseModel):
    batch_no: Code = Field(description="冲红批号（同轮内唯一）")
    invoice_nos: List[InvoiceNo] = Field(min_length=1, description="本批冲红票号（整批退出样本与备选名单）")

    model_config = {
        "json_schema_extra": {"examples": [{"batch_no": "P1", "invoice_nos": ["A1", "B1"]}]}
    }


# ---------------------------------------------------------------- 核查响应模型


class AuditTopic(BaseModel):
    topic_code: str = Field(description="专题代码")
    vacancy_before: int = Field(description="该专题在本批补样前的缺额")


class AuditTopicGroup(BaseModel):
    topics: List[AuditTopic] = Field(description="备选不足的专题组（按登记顺序）")
    vacancy_before_total: int = Field(description="组内各专题补样前缺额合计")
    candidates: List[str] = Field(description="该组当时可用的全部备选票号，跨专题共享的只列一次")
    candidate_count: int = Field(description="备选票号去重后的张数")
    shortfall: int = Field(description="缺额合计 - 备选票数，即该组补不上的名额")


class AuditCheck(BaseModel):
    vacancy_before_total: int = Field(description="专题组缺额合计")
    candidate_count: int = Field(description="专题组备选票数（去重）")
    difference: int = Field(description="缺额合计 - 备选票数")
    round_shortfall: int = Field(description="回执中全轮剩余缺额")
    matches: bool = Field(description="上式与回执剩余缺额是否相等")


class AuditOut(BaseModel):
    round_id: str
    batch_no: str
    seq: int = Field(description="该批次的生效顺序")
    status: Literal["fully_filled", "shortfall", "inconsistent"]
    message: str = Field(description="fully_filled=已补满；shortfall=备选不足仍有缺额")
    round_shortfall: int = Field(description="回执中全轮剩余缺额合计")
    total_filled: int = Field(description="该批实际补上的名额数")
    topic_group: Optional[AuditTopicGroup] = Field(
        default=None, description="无缺额时为 null；有缺额时给出备选不足的专题组"
    )
    check: AuditCheck = Field(description="核等式：缺额合计 - 备选票数 = 剩余缺额")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "round_id": "R2026-01",
                    "batch_no": "P1",
                    "seq": 1,
                    "status": "shortfall",
                    "message": "备选不足，仍有缺额",
                    "round_shortfall": 1,
                    "total_filled": 2,
                    "topic_group": {
                        "topics": [
                            {"topic_code": "价税异常", "vacancy_before": 2},
                            {"topic_code": "进销项背离", "vacancy_before": 1},
                        ],
                        "vacancy_before_total": 3,
                        "candidates": ["C1", "C2", "C3"],
                        "candidate_count": 3,
                        "shortfall": 1,
                    },
                    "check": {
                        "vacancy_before_total": 3,
                        "candidate_count": 3,
                        "difference": 1,
                        "round_shortfall": 1,
                        "matches": True,
                    },
                }
            ]
        }
    }
