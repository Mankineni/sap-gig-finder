"""Agent D — Validator: HTTP checks, deduplication, and expiry detection."""

import asyncio
import json
import re
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx
from playwright.async_api import async_playwright
from rich.console import Console
from rich.table import Table

from agents.models import AnalyzedListing, ValidatedListing
from config.settings import (
    EXPIRED_PHRASES,
    MAX_VALIDATOR_CONCURRENCY,
    VALIDATOR_TIMEOUT,
)

console = Console()

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_TRACKING_PARAMS = re.compile(r"^(utm_\w+|ref|source)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# LAYER 1 — HTTP HEAD check
# ---------------------------------------------------------------------------

async def check_http(url: str) -> tuple[str, int]:
    """Return (status_str, http_code) for *url* via a HEAD request."""
    try:
        async with httpx.AsyncClient(
            timeout=VALIDATOR_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.head(url)
            code = resp.status_code
            if code in (404, 410):
                return ("dead", code)
            if code == 403:
                return ("unknown", 403)
            if 200 <= code <= 302:
                return ("live", code)
            return ("unknown", code)
    except httpx.HTTPError:
        return ("unknown", 0)


# ---------------------------------------------------------------------------
# LAYER 2 — Deduplication
# ---------------------------------------------------------------------------

def _strip_tracking_params(url: str) -> str:
    """Remove utm_*, ref=, source= query parameters from *url*."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {
        k: v for k, v in params.items() if not _TRACKING_PARAMS.match(k)
    }
    new_query = urlencode(cleaned, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _normalise_title(title: str) -> str:
    """Lower-case, collapse whitespace, strip punctuation for comparison."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def deduplicate(listings: list[ValidatedListing]) -> list[ValidatedListing]:
    """Remove duplicates by URL hash and (normalised_title, source) pairs.

    When duplicates exist, keep the entry with the higher score.
    Modifies nothing — returns a new list.
    """
    # Group by canonical URL
    url_groups: dict[str, list[ValidatedListing]] = {}
    for listing in listings:
        canon = _strip_tracking_params(listing.url)
        url_hash = sha256(canon.encode()).hexdigest()
        url_groups.setdefault(url_hash, []).append(listing)

    # Pick the best from each URL group
    best_by_url: dict[str, ValidatedListing] = {}
    for url_hash, group in url_groups.items():
        group.sort(key=lambda l: l.score, reverse=True)
        best_by_url[url_hash] = group[0]

    # Second pass: deduplicate by (normalised_title, source)
    seen_titles: dict[tuple[str, str], ValidatedListing] = {}
    deduped: list[ValidatedListing] = []

    for listing in best_by_url.values():
        key = (_normalise_title(listing.title), listing.source)
        if key in seen_titles:
            existing = seen_titles[key]
            if listing.score > existing.score:
                deduped.remove(existing)
                seen_titles[key] = listing
                deduped.append(listing)
            # else: keep existing, drop this one
        else:
            seen_titles[key] = listing
            deduped.append(listing)

    removed = len(listings) - len(deduped)
    if removed:
        console.log(f"[yellow]Deduplication removed {removed} duplicate(s)")

    return deduped


# ---------------------------------------------------------------------------
# LAYER 3 — Playwright expiry check (GULP / EURSAP only)
# ---------------------------------------------------------------------------

async def check_expired(url: str) -> bool:
    """Open *url* in headless Chromium and look for expiry phrases."""
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=_USER_AGENT)
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            body_text = await page.inner_text("body")
            await browser.close()

            for phrase in EXPIRED_PHRASES:
                if phrase.lower() in body_text.lower():
                    return True
            return False
    except Exception:
        # If we can't check, assume not expired.
        return False


# ---------------------------------------------------------------------------
# Per-listing validation
# ---------------------------------------------------------------------------

async def validate_one(listing: AnalyzedListing) -> ValidatedListing:
    """Run all validation layers on a single listing."""
    status, http_code = await check_http(listing.url)

    if status == "dead":
        return ValidatedListing(
            title=listing.title,
            url=listing.url,
            raw_description=listing.raw_description,
            source=listing.source,
            scraped_at=listing.scraped_at,
            posted_at=listing.posted_at,
            workload_days=listing.workload_days,
            remote_pct=listing.remote_pct,
            is_agency=listing.is_agency,
            tech_stack=listing.tech_stack,
            score=listing.score,
            score_reason=listing.score_reason,
            status="dead",
            http_code=http_code,
        )

    if listing.source in ("gulp", "eursap"):
        if await check_expired(listing.url):
            return ValidatedListing(
                title=listing.title,
                url=listing.url,
                raw_description=listing.raw_description,
                source=listing.source,
                scraped_at=listing.scraped_at,
                posted_at=listing.posted_at,
                workload_days=listing.workload_days,
                remote_pct=listing.remote_pct,
                is_agency=listing.is_agency,
                tech_stack=listing.tech_stack,
                score=listing.score,
                score_reason=listing.score_reason,
                status="expired",
                http_code=http_code,
            )

    return ValidatedListing(
        title=listing.title,
        url=listing.url,
        raw_description=listing.raw_description,
        source=listing.source,
        scraped_at=listing.scraped_at,
        posted_at=listing.posted_at,
        workload_days=listing.workload_days,
        remote_pct=listing.remote_pct,
        is_agency=listing.is_agency,
        tech_stack=listing.tech_stack,
        score=listing.score,
        score_reason=listing.score_reason,
        status="live",
        http_code=http_code,
    )


# ---------------------------------------------------------------------------
# Dead-links logger
# ---------------------------------------------------------------------------

def _log_dead_links(results: list[ValidatedListing]) -> None:
    """Append dead/expired entries to output/dead_links_log.jsonl."""
    dead = [r for r in results if r.status in ("dead", "expired")]
    if not dead:
        return
    with open("output/dead_links_log.jsonl", "a", encoding="utf-8") as fh:
        for entry in dead:
            record = {
                "url": entry.url,
                "title": entry.title,
                "source": entry.source,
                "status": entry.status,
                "http_code": entry.http_code,
                "logged_at": datetime.utcnow().isoformat(),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    console.log(f"[dim]Logged {len(dead)} dead/expired link(s) to dead_links_log.jsonl")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _print_summary(results: list[ValidatedListing]) -> None:
    """Print a rich table summarising validation results by status."""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    table = Table(title="Validation Summary")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")

    status_styles = {
        "live": "green",
        "dead": "red",
        "expired": "yellow",
        "duplicate": "dim",
        "unknown": "cyan",
    }

    for status in ("live", "dead", "expired", "duplicate", "unknown"):
        if status in counts:
            table.add_row(
                f"[{status_styles.get(status, '')}]{status}",
                str(counts[status]),
            )

    console.print(table)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_validators(
    analyzed_queue: asyncio.Queue,
    results: list,
    lock: asyncio.Lock,
    analyst_sentinel: object,
) -> None:
    """Spin up validator workers that drain analyzed_queue into *results*."""
    done_event = asyncio.Event()

    async def _worker(worker_id: int) -> None:
        while True:
            item = await analyzed_queue.get()

            if item is analyst_sentinel:
                if not done_event.is_set():
                    done_event.set()
                # Re-queue sentinel so other blocked workers wake up and exit.
                await analyzed_queue.put(analyst_sentinel)
                analyzed_queue.task_done()
                return

            if done_event.is_set():
                # Re-queue sentinel for any remaining workers still blocked.
                await analyzed_queue.put(analyst_sentinel)
                analyzed_queue.task_done()
                return

            listing: AnalyzedListing = item
            try:
                validated = await validate_one(listing)
            except Exception as exc:
                console.log(
                    f"[red]Validator worker {worker_id} error on "
                    f"'{listing.title}': {exc}"
                )
                analyzed_queue.task_done()
                continue

            async with lock:
                results.append(validated)

            status_style = {
                "live": "green",
                "dead": "red",
                "expired": "yellow",
                "unknown": "cyan",
            }.get(validated.status, "")
            console.log(
                f"[{status_style}]\\[{validated.status}] {validated.title}"
            )

            analyzed_queue.task_done()

    workers = [_worker(i) for i in range(MAX_VALIDATOR_CONCURRENCY)]
    await asyncio.gather(*workers)

    # Post-processing: deduplicate in-place.
    deduped = deduplicate(results)
    results.clear()
    results.extend(deduped)

    # Log dead/expired links and print summary.
    _log_dead_links(results)
    _print_summary(results)
