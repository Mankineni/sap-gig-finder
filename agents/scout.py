"""Agent A — Scout: parallel workers that scrape sources and push RawListings into a queue."""

import asyncio
import re
import traceback
from pathlib import Path
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

_DEBUG_DIR = Path("debug")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "scout"


async def _save_debug(page, label: str) -> None:
    """Dump current page HTML + screenshot + URL for post-mortem analysis."""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        slug = _slug(label)
        html_path = _DEBUG_DIR / f"{slug}.html"
        png_path = _DEBUG_DIR / f"{slug}.png"
        meta_path = _DEBUG_DIR / f"{slug}.txt"

        try:
            html = await page.content()
        except Exception as exc:
            html = f"<!-- page.content() failed: {exc} -->"
        html_path.write_text(html, encoding="utf-8", errors="replace")

        try:
            await page.screenshot(path=str(png_path), full_page=True)
        except Exception as exc:
            console.log(f"[yellow]\\[{label}] screenshot failed: {exc}")

        try:
            meta = f"url={page.url}\ntitle={await page.title()}\n"
        except Exception as exc:
            meta = f"meta capture failed: {exc}\n"
        meta_path.write_text(meta, encoding="utf-8", errors="replace")

        console.log(f"[cyan]\\[{label}] debug artifacts written to {html_path.parent}/")
    except Exception as exc:
        console.log(f"[red]\\[{label}] _save_debug itself failed: {exc}")


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
            console.log(f"[dim]{traceback.format_exc()}[/dim]")
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF * attempt)
    console.log(f"[red]\\[{label}] all {_MAX_RETRIES} attempts exhausted")
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Source: Freelancermap (Playwright — RSS no longer available)
# ---------------------------------------------------------------------------

async def scout_freelancermap(queue: asyncio.Queue, query: str) -> int:
    """Scrape Freelancermap project listings for *query* via Playwright.

    The listing page only shows titles, so we visit each project detail
    page to grab the full description for better Claude scoring.
    """
    label = f"freelancermap|{query[:30]}"
    encoded = quote_plus(query)
    url = f"https://www.freelancermap.de/projekte?query={encoded}"

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
            console.log(f"[dim]\\[{label}] matched {len(links)} /projekt/ links")

            # Collect unique hrefs + titles from listing page.
            candidates: list[tuple[str, str]] = []
            seen_hrefs: set[str] = set()

            for link in links:
                href = await link.get_attribute("href") or ""
                if not href or href in seen_hrefs:
                    continue

                text = (await link.inner_text()).strip()
                if len(text) < 10:
                    continue

                seen_hrefs.add(href)
                full_url = (
                    f"https://www.freelancermap.de{href}"
                    if href.startswith("/")
                    else href
                )
                candidates.append((full_url, text))

            if not candidates:
                await _save_debug(page, label)

            # Visit each detail page (up to 15) for the full description.
            results: list[RawListing] = []
            for project_url, title in candidates[:15]:
                desc = ""
                try:
                    detail = await context.new_page()
                    await detail.goto(
                        project_url,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    await detail.wait_for_timeout(2_000)
                    body = await detail.inner_text("main")
                    # Extract text after "Beschreibung" heading if present.
                    if "Beschreibung" in body:
                        desc = body.split("Beschreibung", 1)[1].strip()[:1000]
                    else:
                        desc = body[:1000]
                    await detail.close()
                except Exception:
                    pass  # Keep empty desc; title alone may suffice.

                results.append(
                    RawListing(
                        title=title,
                        url=project_url,
                        raw_description=desc,
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
            console.log(f"[dim]\\[{label}] matched {len(cards)} .card nodes")

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

            if not results:
                await _save_debug(page, label)

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
            console.log(f"[dim]\\[{label}] matched {len(links)} /jobs/sap links")

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

            if not results:
                await _save_debug(page, label)

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
    """Scrape LinkedIn public job search (no login required)."""
    label = f"linkedin|{query[:30]}"
    encoded = quote_plus(query)
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={encoded}&location=Germany&f_TPR=r604800"
    )

    async def _scrape():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(5_000)

            # Scroll down to load more results.
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1_500)

            cards = await page.query_selector_all(".base-card")
            console.log(f"[dim]\\[{label}] matched {len(cards)} .base-card nodes")

            results: list[RawListing] = []
            seen_hrefs: set[str] = set()

            for card in cards:
                link_el = await card.query_selector("a[href]")
                if not link_el:
                    continue

                href = await link_el.get_attribute("href") or ""
                # Strip tracking params from LinkedIn URLs.
                clean_href = href.split("?")[0] if href else ""
                if not clean_href or clean_href in seen_hrefs:
                    continue
                seen_hrefs.add(clean_href)

                text = (await card.inner_text()).strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                title = lines[0] if lines else ""
                # Lines typically: title, title (dup), company, location, date
                company = lines[2] if len(lines) > 2 else ""
                location = lines[3] if len(lines) > 3 else ""
                desc = f"Company: {company}. Location: {location}."

                if title and len(title) > 5:
                    results.append(
                        RawListing(
                            title=title,
                            url=clean_href,
                            raw_description=desc,
                            source="linkedin",
                        )
                    )

            if not results:
                await _save_debug(page, label)

            await browser.close()
            return results

    listings = await _retry(_scrape, label)

    for listing in listings:
        await queue.put(listing)

    return len(listings)


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
            console.log(f"[dim]{traceback.format_exc()}[/dim]")
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
