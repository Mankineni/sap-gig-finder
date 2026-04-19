"""Pipeline orchestrator — wires Scout → Analyst → Validator → Formatter."""

import asyncio
import argparse

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from agents.scout import run_scouts, SENTINEL as SCOUT_SENTINEL
from agents.analyst import run_analysts
from agents.validator import run_validators
from agents.formatter import write_outputs
from config.settings import (
    QUEUE_MAX_SIZE,
    MAX_ANALYST_CONCURRENCY,
    MAX_VALIDATOR_CONCURRENCY,
    PIPELINE_TIMEOUT_SECONDS,
    MIN_SCORE,
)

console = Console()


async def pipeline(dry_run: bool = False):
    raw_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    analyzed_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    results: list = []
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_ANALYST_CONCURRENCY)
    stats = {
        "raw_count": 0,
        "analyzed_count": 0,
        "dead_count": 0,
        "dup_count": 0,
        "final_count": 0,
    }

    ANALYST_SENTINEL = object()

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        console=console,
    ) as progress:
        t1 = progress.add_task("Scouting...", total=None)
        t2 = progress.add_task("Analysing...", total=None)
        t3 = progress.add_task("Validating...", total=None)

        async def scouts():
            counts = await run_scouts(raw_queue)
            stats["raw_count"] = sum(counts.values())
            progress.update(
                t1, description=f"Scout done — {stats['raw_count']} raw"
            )

        async def analysts():
            await run_analysts(
                raw_queue,
                analyzed_queue,
                semaphore,
                SCOUT_SENTINEL,
                ANALYST_SENTINEL,
            )
            stats["analyzed_count"] = analyzed_queue.qsize()
            progress.update(
                t2,
                description=f"Analyst done — {stats['analyzed_count']} kept",
            )

        async def validators():
            if dry_run:
                progress.update(t3, description="Validator skipped (dry-run)")
                return
            await run_validators(
                analyzed_queue, results, lock, ANALYST_SENTINEL
            )
            stats["dead_count"] = sum(
                1 for r in results if r.status in ("dead", "expired")
            )
            stats["dup_count"] = sum(
                1 for r in results if r.status == "duplicate"
            )
            stats["final_count"] = sum(
                1 for r in results if r.status == "live"
            )
            progress.update(
                t3,
                description=f"Validator done — {stats['final_count']} live",
            )

        await asyncio.gather(scouts(), analysts(), validators())

    if not dry_run:
        write_outputs(results, stats)
    else:
        console.print("[yellow]Dry-run: skipping file output[/yellow]")
        for r in results[:5]:
            console.print(f"  [{r.score}] {r.title} — {r.source}")

    if stats["raw_count"] == 0:
        console.print(
            "[red]Pipeline produced 0 raw listings — every scout returned empty.[/red]"
        )
        console.print(
            "[red]Check debug/ artifacts for per-source page dumps.[/red]"
        )
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(
        description="SAP Gig Finder — multi-agent scraping pipeline"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run scouts and analysts but skip validation and file output",
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            asyncio.wait_for(
                pipeline(dry_run=args.dry_run),
                timeout=PIPELINE_TIMEOUT_SECONDS,
            )
        )
    except asyncio.TimeoutError:
        console.print("[red]Pipeline timed out[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
