from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RawListing:
    title: str
    url: str
    raw_description: str
    source: str          # gulp | freelancermap | linkedin | upwork | eursap
    scraped_at: datetime = field(default_factory=datetime.utcnow)
    posted_at: Optional[datetime] = None   # real posting date from the source, if extracted


@dataclass
class AnalyzedListing(RawListing):
    workload_days: Optional[int] = None   # 1-5, None if not mentioned
    remote_pct: int = 0                   # 0-100
    is_agency: bool = True
    tech_stack: list[str] = field(default_factory=list)
    score: int = 0
    score_reason: str = ""


@dataclass
class ValidatedListing(AnalyzedListing):
    status: str = "unknown"   # live | dead | duplicate | expired | unknown
    http_code: Optional[int] = None
    verified_at: datetime = field(default_factory=datetime.utcnow)
