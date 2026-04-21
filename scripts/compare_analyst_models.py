"""Compare analyst output between two Claude models on the same listings.

Usage:
    python -m scripts.compare_analyst_models

Reads fixture listings from docs/gigs_latest.json (falls back to a hardcoded
sample), sends each through both MODEL_A and MODEL_B via the existing
analyze_one() helper, and prints a side-by-side diff of the extracted fields.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import anthropic

from agents.analyst import analyze_one
from agents.models import RawListing
from config.settings import ANTHROPIC_API_KEY

MODEL_A = "claude-sonnet-4-20250514"
MODEL_B = "claude-haiku-4-5-20251001"

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "docs" / "gigs_latest.json"


def load_listings(n: int = 5) -> list[RawListing]:
    if not SAMPLE_PATH.exists():
        raise SystemExit(f"No fixture file at {SAMPLE_PATH}")
    data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    if not data:
        raise SystemExit("Fixture file is empty — nothing to compare")
    listings = []
    for item in data[:n]:
        listings.append(RawListing(
            title=item["title"],
            url=item["url"],
            raw_description=item["raw_description"],
            source=item["source"],
            scraped_at=datetime.fromisoformat(item["scraped_at"]),
        ))
    return listings


async def run_one(model: str, listing: RawListing) -> dict:
    import agents.analyst as analyst_mod
    orig = analyst_mod.ANTHROPIC_MODEL if hasattr(analyst_mod, "ANTHROPIC_MODEL") else None

    import config.settings as s
    s.ANTHROPIC_MODEL = model
    analyst_mod.ANTHROPIC_MODEL = model

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    sem = asyncio.Semaphore(1)
    result = await analyze_one(listing, client, sem)

    if orig is not None:
        analyst_mod.ANTHROPIC_MODEL = orig

    return {
        "workload_days": result.workload_days,
        "remote_pct": result.remote_pct,
        "is_agency": result.is_agency,
        "tech_stack": result.tech_stack,
        "score": result.score,
        "score_reason": result.score_reason,
    }


def fmt(v) -> str:
    if isinstance(v, list):
        return ", ".join(v) if v else "—"
    return str(v) if v is not None else "—"


async def main():
    listings = load_listings(5)
    print(f"Comparing {MODEL_A}  vs  {MODEL_B}")
    print(f"Listings: {len(listings)}\n")

    for i, listing in enumerate(listings, 1):
        print(f"─── [{i}] {listing.title[:80]}")
        a, b = await asyncio.gather(
            run_one(MODEL_A, listing),
            run_one(MODEL_B, listing),
        )
        keys = ["workload_days", "remote_pct", "is_agency", "tech_stack", "score"]
        print(f"    {'field':<15} {'sonnet':<35} {'haiku':<35} match?")
        for k in keys:
            va, vb = fmt(a[k]), fmt(b[k])
            match = "✓" if a[k] == b[k] else "✗"
            print(f"    {k:<15} {va[:33]:<35} {vb[:33]:<35} {match}")
        print(f"    reason[sonnet]: {a['score_reason']}")
        print(f"    reason[haiku] : {b['score_reason']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
