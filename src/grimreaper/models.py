from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class Resource:
    """A billable AWS resource discovered by a scanner (never by the LLM)."""

    kind: str
    id: str
    region: str
    name: str = ""
    monthly_cost: float = 0.0  # rough list-price estimate, USD
    detail: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    deletable: bool = True  # False = GrimReaper reports it but won't delete it

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.region}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Resource:
        return cls(**data)


class Verdict(BaseModel):
    resource_key: str = Field(description="Exact key of a resource returned by the inventory auditor.")
    action: Literal["delete", "keep", "review"]
    reason: str = Field(description="One or two sentences a human can act on.")
    est_monthly_savings: float = Field(description="USD per month saved if deleted, 0 if unknown.")


class ReapingReport(BaseModel):
    summary: str = Field(description="Plain-English explanation of where the money is going.")
    observed_monthly_spend: float = Field(description="Usage cost (before credits) over the last 30 days.")
    verdicts: list[Verdict]
