"""Agent A — Scout: parallel workers that scrape sources and push RawListings into a queue."""

import asyncio
from urllib.parse import quote_plus

import feedparser
from playwright.async_api import async_playwright
from rich.console import Console

from agents.models import RawListing
from config.settings import SEARCH_QUERIES

console = Console()

# Downstream consumers watch for this object to know scouting is done.
SENTINEL = object()

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _retry(coro_factory, label: str):
    """Call *coro_factory()* up to _MAX_RETRIES times with exponential back-off.

    *coro_factory* must be a zero-arg callable that returns a new awaitable
    each time (so we can retry without reusing an exhausted coroutine).
    """
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            console.log(
                f"[yellow]\\[{label}] attempt {attempt}/{_MAX_RETRIES} failed: {exc}"
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF * attempt)
    console.log(f"[red]\\[{label}] all {_MAX_RETRIES} attempts exhausted")
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Source: Freelancermap (RSS)
# ---------------------------------------------------------------------------

async def scout_freelancermap(queue: asyncio.Queue, query: str) -> int:
    """Fetch the Freelancermap RSS feed for *query* and enqueue RawListings."""
    label = f"freelancermap|{query[:30]}"
    encoded = quote_plus(query)
    url = (
        f"https://www.freelancermap.de/projektboerse.html"
        f"?query={encoded}&remote=1&format=rss"
    )

    def _parse():
        """Synchronous feedparser call wrapped for the executor."""
        return feedparser.parse(url)

    feed = await _retry(
        lambda: asyncio.get_running_loop().run_in_executor(None, _parse),
        label,
    )

    count = 0
    for entry in feed.entries:
        listing = RawListing(
            title=entry.get("title", ""),
            url=entry.get("link", ""),
            raw_description=entry.get("summary", entry.get("description", "")),
            source="freelancermap",
        )
        await queue.put(listing)
        count += 1

    return count


# ---------------------------------------------------------------------------
# Source: GULP (Playwright)
# ---------------------------------------------------------------------------

async def scout_gulp(queue: asyncio.Queue, query: str) -> int:
    """Scrape GULP project listings for *query* and enqueue RawListings."""
    label = f"gulp|{query[:30]}"
    encoded = quote_plus(query)
    url = f"https://www.gulp.de/gulp2/g/projekte?query={encoded}"

    async def _scrape():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                locale="de-DE",
                extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for project cards to render (GULP uses various class names).
            try:
                await page.wait_for_selector(
                    "[class*='project-list-item'], [class*='ProjectList'], article",
                    timeout=15_000,
                )
            except Exception:
                # Page may have loaded but with zero results.
                pass

            cards = await page.query_selector_all(
                "[class*='project-list-item'], [class*='ProjectList'] a, article"
            )

            results: list[RawListing] = []
            for card in cards:
                link_el = await card.query_selector("a[href]")
                href = await link_el.get_attribute("href") if link_el else ""
                if href and not href.startswith("http"):
                    href = f"https://www.gulp.de{href}"

                title_el = (
                    await card.query_selector("h2, h3, [class*='title'], a")
                )
                title = (await title_el.inner_text()).strip() if title_el else ""

                desc_el = await card.query_selector(
                    "p, [class*='description'], [class*='snippet']"
                )
                desc = (await desc_el.inner_text()).strip() if desc_el else ""

                if title:
                    results.append(
                        RawListing(
                            title=title,
                            url=href or url,
                            raw_description=desc,
                            source="gulp",
                        )
                    )

            await browser.close()
            return results

    listings = await _retry(_scrape, label)

    for listing in listings:
        await queue.put(listing)

    return len(listings)


# ---------------------------------------------------------------------------
# Source: EURSAP (Playwright)
# ---------------------------------------------------------------------------

async def scout_eursap(queue: asyncio.Queue) -> int:
    """Scrape EURSAP job board and enqueue RawListings."""
    label = "eursap"

    async def _scrape():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                locale="en-GB",
            )
            page = await context.new_page()
            await page.goto(
                "https://eursap.eu/jobs/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            try:
                await page.wait_for_selector(
                    ".job_listing, article, [class*='job'], .listing",
                    timeout=15_000,
                )
            except Exception:
                pass

            cards = await page.query_selector_all(
                ".job_listing, article, [class*='job-item'], .listing"
            )

            results: list[RawListing] = []
            for card in cards:
                link_el = await card.query_selector("a[href]")
                href = await link_el.get_attribute("href") if link_el else ""

                title_el = await card.query_selector(
                    "h2, h3, h4, [class*='title'], a"
                )
                title = (await title_el.inner_text()).strip() if title_el else ""

                desc_el = await card.query_selector(
                    "p, [class*='description'], [class*='snippet'], .content"
                )
                desc = (await desc_el.inner_text()).strip() if desc_el else ""

                if title:
                    results.append(
                        RawListing(
                            title=title,
                            url=href or "https://eursap.eu/jobs/",
                            raw_description=desc,
                            source="eursap",
                        )
                    )

            await browser.close()
            return results

    listings = await _retry(_scrape, label)

    for listing in listings:
        await queue.put(listing)

    return len(listings)


# ---------------------------------------------------------------------------
# Source stubs
# ---------------------------------------------------------------------------

async def scout_linkedin(queue: asyncio.Queue, query: str) -> int:
    """Placeholder — LinkedIn scraping requires RapidAPI integration."""
    console.log("[dim]LinkedIn scout not yet implemented[/dim]")
    return 0


async def scout_upwork(queue: asyncio.Queue, query: str) -> int:
    """Placeholder — Upwork scraping requires OAuth integration."""
    console.log("[dim]Upwork scout not yet implemented[/dim]")
    return 0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_scouts(queue: asyncio.Queue) -> dict:
    """Launch every scout concurrently, push SENTINEL when done, return counts."""
    counts: dict[str, int] = {
        "gulp": 0,
        "freelancermap": 0,
        "linkedin": 0,
        "upwork": 0,
        "eursap": 0,
    }

    async def _run_source(name: str, coro) -> tuple[str, int]:
        try:
            n = await coro
            console.log(f"[green]\\[{name}] finished — {n} listing(s)")
            return name, n
        except Exception as exc:
            console.log(f"[red]\\[{name}] failed permanently: {exc}")
            return name, 0

    tasks = []

    # Query-based sources: one task per (source, query) pair.
    for query in SEARCH_QUERIES:
        tasks.append(
            _run_source(
                "freelancermap",
                scout_freelancermap(queue, query),
            )
        )
        tasks.append(_run_source("gulp", scout_gulp(queue, query)))
        tasks.append(_run_source("linkedin", scout_linkedin(queue, query)))
        tasks.append(_run_source("upwork", scout_upwork(queue, query)))

    # EURSAP has no query parameter — only one task.
    tasks.append(_run_source("eursap", scout_eursap(queue)))

    results = await asyncio.gather(*tasks)

    for name, n in results:
        counts[name] = counts.get(name, 0) + n

    # Signal downstream that scouting is complete.
    await queue.put(SENTINEL)
    console.log("[bold cyan]All scouts finished — SENTINEL pushed to queue")

    return counts
