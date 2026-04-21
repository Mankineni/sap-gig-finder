"""Agent B — Analyst: reads RawListings, calls Claude to score them, outputs AnalyzedListings."""

import asyncio
import json

import anthropic
from rich.console import Console

from agents.models import RawListing, AnalyzedListing
from config.settings import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    MAX_ANALYST_CONCURRENCY,
    MIN_SCORE,
)

console = Console()

ANALYST_SYSTEM_PROMPT = """\
You are an expert SAP freelance market analyst.
Extract structured data from the job description below.
Return ONLY valid JSON with exactly these keys:
{
  "workload_days": <integer 1-5 or null>,
  "remote_pct": <integer 0-100>,
  "is_agency": <true if posted by recruiter/agency, false if direct client>,
  "tech_stack": [<SAP technologies mentioned>],
  "score": <integer 1-10>,
  "score_reason": "<one sentence>"
}
Scoring:
+3 if tech_stack contains Datasphere, SAC, or BW/4HANA
+2 if remote_pct >= 80
+2 if workload_days is not null and <= 3
+1 if is_agency is false
+2 if description mentions AI, LLM, or automation
-2 if only generic SAP Basis or SAP support with no modern stack
Do not include any text outside the JSON object."""

async def analyze_one(
    listing: RawListing,
    client: anthropic.AsyncAnthropic,
    semaphore: asyncio.Semaphore,
) -> AnalyzedListing:
    """Send a single listing to Claude and return an AnalyzedListing."""
    user_message = (
        f"Title: {listing.title}\n"
        f"Source: {listing.source}\n"
        f"URL: {listing.url}\n\n"
        f"Description:\n{listing.raw_description}"
    )

    async with semaphore:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=ANALYST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

    raw_text = response.content[0].text.strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        # Try to extract JSON from the response if Claude added extra text.
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(raw_text[start:end])
            except json.JSONDecodeError:
                data = None
        else:
            data = None

    if data is None:
        return AnalyzedListing(
            title=listing.title,
            url=listing.url,
            raw_description=listing.raw_description,
            source=listing.source,
            scraped_at=listing.scraped_at,
            posted_at=listing.posted_at,
            score=0,
            score_reason="parse error",
        )

    # Claude occasionally returns `"score": null` or omits numeric fields —
    # coerce to safe defaults so downstream comparisons don't hit TypeError.
    def _int_or(v, default):
        return default if v is None else v

    return AnalyzedListing(
        title=listing.title,
        url=listing.url,
        raw_description=listing.raw_description,
        source=listing.source,
        scraped_at=listing.scraped_at,
        posted_at=listing.posted_at,
        workload_days=data.get("workload_days"),  # None allowed — represents "flexible"
        remote_pct=_int_or(data.get("remote_pct"), 0),
        is_agency=data.get("is_agency") if data.get("is_agency") is not None else True,
        tech_stack=data.get("tech_stack") or [],
        score=_int_or(data.get("score"), 0),
        score_reason=data.get("score_reason") or "",
    )


async def run_analysts(
    raw_queue: asyncio.Queue,
    analyzed_queue: asyncio.Queue,
    semaphore: asyncio.Semaphore,
    scout_sentinel: object,
    analyst_sentinel: object,
) -> None:
    """Spin up analyst workers that drain raw_queue and fill analyzed_queue."""
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    analysts_done = asyncio.Event()

    async def _worker(worker_id: int) -> None:
        while True:
            item = await raw_queue.get()

            if item is scout_sentinel:
                # First worker to see the sentinel signals everyone else.
                if not analysts_done.is_set():
                    analysts_done.set()
                    await analyzed_queue.put(analyst_sentinel)
                    console.log(
                        f"[bold cyan]Analyst worker {worker_id} pushed "
                        "ANALYST_SENTINEL to analyzed_queue"
                    )
                # Re-queue sentinel so other blocked workers wake up and exit.
                await raw_queue.put(scout_sentinel)
                raw_queue.task_done()
                return

            # Another worker already handled the sentinel — exit cleanly.
            if analysts_done.is_set():
                # Re-queue sentinel for any remaining workers still blocked.
                await raw_queue.put(scout_sentinel)
                raw_queue.task_done()
                return

            listing: RawListing = item
            try:
                result = await analyze_one(listing, client, semaphore)
            except Exception as exc:
                console.log(
                    f"[red]Analyst worker {worker_id} error on "
                    f"'{listing.title}': {exc}"
                )
                raw_queue.task_done()
                continue

            if result.score >= MIN_SCORE:
                await analyzed_queue.put(result)
                console.log(
                    f"[green]\\[score {result.score}] {result.title}"
                )
            else:
                console.log(
                    f"[dim]Filtered out: {result.title} "
                    f"(score {result.score})[/dim]"
                )

            raw_queue.task_done()

    workers = [
        _worker(i) for i in range(MAX_ANALYST_CONCURRENCY)
    ]
    await asyncio.gather(*workers)
