"""Agent A — Scout: parallel workers that scrape sources and push RawListings into a queue."""

import asyncio
from urllib.parse import quote_plus

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
    """Call *coro_factory()* up to _MAX_RETRIES times with exponential back-off."""
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
# Source: Freelancermap (Playwright — RSS no longer available)
# ---------------------------------------------------------------------------

async def scout_freelancermap(queue: asyncio.Queue, query: str) -> int:
    """Scrape Freelancermap project listings for *query* via Playwright."""
    label = f"freelancermap|{query[:30]}"
    encoded = quote_plus(query)
    url = f"https://www.freelancermap.de/projektboerse.html?query={encoded}&remote=1"

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
            await page.wait_for_timeout(5_000)

            links = await page.query_selector_all('a[href*="/projekt/"]')

            results: list[RawListing] = []
            seen_hrefs: set[str] = set()

            for link in links:
                href = await link.get_attribute("href") or ""
                if not href or href in seen_hrefs:
                    continue

                text = (await link.inner_text()).strip()
                if len(text) < 10:
                    continue

                seen_hrefs.add(href)
                full_url = f"https://www.freelancermap.de{href}" if href.startswith("/") else href

                results.append(
                    RawListing(
                        title=text,
                        url=full_url,
                        raw_description="",
                        source="freelancermap",
                    )
                )

            await browser.close()
            return results

    listings = await _retry(_scrape, label)

    for listing in listings:
        await queue.put(listing)

    await asyncio.sleep(1)  # rate-limit between queries
    return len(listings)


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

            try:
                await page.wait_for_selector(".card", timeout=15_000)
            except Exception:
                pass

            await page.wait_for_timeout(3_000)
            cards = await page.query_selector_all(".card")

            results: list[RawListing] = []
            for card in cards:
                link_el = await card.query_selector("a[href*=projekt]")
                if not link_el:
                    continue

                href = await link_el.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = f"https://www.gulp.de{href}"

                title_el = await card.query_selector(
                    "app-heading-tag, [class*='gp-title'], h2, h3"
                )
                title = (await title_el.inner_text()).strip() if title_el else ""

                desc_el = await card.query_selector(
                    "[class*='description'], p, .text-truncate"
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
                "https://eursap.eu/sap-jobs/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_timeout(5_000)

            links = await page.query_selector_all('a[href*="/jobs/sap"]')

            results: list[RawListing] = []
            seen_hrefs: set[str] = set()

            for link in links:
                href = await link.get_attribute("href") or ""
                if not href or href in seen_hrefs:
                    continue

                text = (await link.inner_text()).strip()
                # Clean up multiline text from the card
                title_line = text.split("\n")[0].strip() if text else ""
                # Extract description from the rest
                desc = " ".join(text.split("\n")[1:]).strip() if "\n" in text else ""

                if not title_line or len(title_line) < 5:
                    continue

                # Remove "SAP JOB VACANCY:" prefix if present
                if title_line.upper().startswith("SAP JOB VACANCY:"):
                    title_line = title_line[len("SAP JOB VACANCY:"):].strip()

                seen_hrefs.add(href)
                results.append(
                    RawListing(
                        title=title_line,
                        url=href,
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
