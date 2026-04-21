"""Agent A — Scout: parallel workers that scrape sources and push RawListings into a queue."""

import asyncio
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
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
# Posting-date extraction
# ---------------------------------------------------------------------------

_DE_MONTHS = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mar": 3,
    "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
_EN_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}


# Keywords that, when present just before a date, strongly indicate the
# posting date (as opposed to a project start date, activity timestamp,
# or unrelated prose-date mention).
_POSTING_KEYWORDS = (
    "veröffentlicht", "veroeffentlicht", "eingestellt", "online seit",
    "publiziert", "erstellt am", "einstellungsdatum",
    "posted on", "posted", "published", "listed on", "date posted",
)


def _within_range(dt: datetime, now: datetime) -> bool:
    cutoff = now - timedelta(days=180)
    return cutoff <= dt <= now + timedelta(days=1)


def _try_absolute_near(window: str, now: datetime) -> Optional[datetime]:
    """Extract the first plausible absolute date from a small text window."""
    # DE numeric: 15.04.2026
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", window)
    if m:
        try:
            dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if _within_range(dt, now): return dt
        except ValueError: pass
    # ISO: 2026-04-15
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})(?!\d)", window)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if _within_range(dt, now): return dt
        except ValueError: pass
    # DE month names: 15. April 2026
    m = re.search(r"\b(\d{1,2})\.?\s+([a-zäA-ZÄ]+)\s+(20\d{2})\b", window)
    if m:
        mon = _DE_MONTHS.get(m.group(2).lower()) or _EN_MONTHS.get(m.group(2).lower())
        if mon:
            try:
                dt = datetime(int(m.group(3)), mon, int(m.group(1)))
                if _within_range(dt, now): return dt
            except ValueError: pass
    # EN month names: April 15, 2026
    m = re.search(r"\b([a-zA-Z]+)\s+(\d{1,2}),?\s+(20\d{2})\b", window)
    if m:
        mon = _EN_MONTHS.get(m.group(1).lower())
        if mon:
            try:
                dt = datetime(int(m.group(3)), mon, int(m.group(2)))
                if _within_range(dt, now): return dt
            except ValueError: pass
    return None


def _try_relative_near(window: str, now: datetime) -> Optional[datetime]:
    """Extract a relative date (N days ago, vor N Tagen, heute, ...)."""
    lower = window.lower()
    m = re.search(r"\b(\d+)\s+(minute|minuten|hour|hours|std|stunde|stunden|day|days|tag|tage|tagen|week|weeks|woche|wochen|month|months|monat|monate|monaten)\b", lower)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith(("minute", "minut")):       delta = timedelta(minutes=n)
        elif unit.startswith(("hour", "std", "stund")): delta = timedelta(hours=n)
        elif unit.startswith(("day", "tag")):           delta = timedelta(days=n)
        elif unit.startswith(("week", "woch")):         delta = timedelta(weeks=n)
        else:                                           delta = timedelta(days=30 * n)
        dt = now - delta
        if _within_range(dt, now): return dt
    if re.search(r"\b(heute|today)\b", lower):
        return now
    if re.search(r"\b(gestern|yesterday)\b", lower):
        return now - timedelta(days=1)
    return None


def _parse_posting_date(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Extract a listing's posting date from raw page text.

    Strategy, in order:
      1. Keyword-anchored match — look for a date within ~40 chars *after*
         a posting keyword like "veröffentlicht am" / "posted on". This is
         by far the most reliable signal and runs first.
      2. Fallback — collect all plausible absolute dates in the past 180
         days and pick the oldest (posting dates are usually older than
         activity/updated timestamps on the same page).

    Returns None if nothing plausible is found.
    """
    if not text:
        return None
    now = now or datetime.utcnow()

    # (1) Keyword-anchored extraction.
    lower = text.lower()
    for kw in _POSTING_KEYWORDS:
        idx = lower.find(kw)
        while idx != -1:
            window = text[idx:idx + len(kw) + 80]
            dt = _try_absolute_near(window, now) or _try_relative_near(window, now)
            if dt is not None:
                return dt
            idx = lower.find(kw, idx + len(kw))

    # (2) Fallback — collect all plausible absolute dates, pick the oldest.
    # Oldest rather than newest: a listing page usually has the posting
    # date as the oldest date on the page (activity/updated timestamps
    # are more recent, project start dates are filtered out as future).
    candidates: list[datetime] = []
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", text):
        try:
            dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if _within_range(dt, now): candidates.append(dt)
        except ValueError: pass
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})(?!\d)", text):
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if _within_range(dt, now): candidates.append(dt)
        except ValueError: pass

    if not candidates:
        return None
    return min(candidates)


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

            # Fetch up to 15 detail pages in parallel (semaphore caps concurrency
            # to 5 to avoid tripping bot detection).
            sem = asyncio.Semaphore(5)

            async def _fetch_detail(project_url: str, title: str) -> RawListing:
                desc = ""
                posted_at = None
                async with sem:
                    try:
                        detail = await context.new_page()
                        await detail.goto(
                            project_url,
                            wait_until="domcontentloaded",
                            timeout=20_000,
                        )
                        await detail.wait_for_timeout(2_000)
                        body = await detail.inner_text("main")
                        if "Beschreibung" in body:
                            desc = body.split("Beschreibung", 1)[1].strip()[:1000]
                        else:
                            desc = body[:1000]
                        # Posting date tends to live near the top of detail pages.
                        posted_at = _parse_posting_date(body[:2000])
                        await detail.close()
                    except Exception:
                        pass  # Keep empty desc; title alone may suffice.

                return RawListing(
                    title=title,
                    url=project_url,
                    raw_description=desc,
                    source="freelancermap",
                    posted_at=posted_at,
                )

            results = await asyncio.gather(*[
                _fetch_detail(u, t) for u, t in candidates[:15]
            ])

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

                # Gulp cards embed a date somewhere in their inner text.
                card_text = await card.inner_text()
                posted_at = _parse_posting_date(card_text)

                if title:
                    results.append(
                        RawListing(
                            title=title,
                            url=href or url,
                            raw_description=desc,
                            source="gulp",
                            posted_at=posted_at,
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

                posted_at = _parse_posting_date(text)

                seen_hrefs.add(href)
                results.append(
                    RawListing(
                        title=title_line,
                        url=href,
                        raw_description=desc,
                        source="eursap",
                        posted_at=posted_at,
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

                posted_at = _parse_posting_date(text)

                if title and len(title) > 5:
                    results.append(
                        RawListing(
                            title=title,
                            url=clean_href,
                            raw_description=desc,
                            source="linkedin",
                            posted_at=posted_at,
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
