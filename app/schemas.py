"""请求模型（Swagger 入参）。"""
from __future__ import annotations

from typing import Annotated, List

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
