"""Agent C — Formatter: builds Markdown reports and JSON output from validated listings."""

import json
import shutil
from dataclasses import asdict
from datetime import date, datetime

from rich.console import Console

from agents.models import ValidatedListing

console = Console()


def _days_label(days: int | None) -> str:
    return str(days) if days is not None else "Flexible"


def _skills_label(tech_stack: list[str]) -> str:
    return ", ".join(tech_stack[:3]) if tech_stack else "—"


def _build_table(listings: list[ValidatedListing]) -> str:
    """Return a Markdown table of listings."""
    rows = [
        "| Job Title | Source | Link | Days/week | Remote | Skills |",
        "|---|---|---|---|---|---|",
    ]
    for l in listings:
        title = l.title.replace("|", "\\|")
        rows.append(
            f"| {title} "
            f"| {l.source} "
            f"| [Apply]({l.url}) "
            f"| {_days_label(l.workload_days)} "
            f"| {l.remote_pct}% "
            f"| {_skills_label(l.tech_stack)} |"
        )
    return "\n".join(rows)


def format_report(listings: list[ValidatedListing], stats: dict) -> str:
    """Build a full Markdown report from validated listings and pipeline stats."""
    live = sorted(
        [l for l in listings if l.status == "live"],
        key=lambda l: l.score,
        reverse=True,
    )

    top_picks = [l for l in live if l.score >= 8]
    rest = [l for l in live if l.score < 8]
    today = date.today().isoformat()
    live_count = len(live)

    sections = [
        f"# SAP Gig Radar — {today}",
        f'> {live_count} verified opportunities · scraped {stats["raw_count"]}'
        f' · {stats["dead_count"]} dead links removed',
        "",
    ]

    # Top picks
    sections.append("## Top picks  (score 8-10)\n")
    if top_picks:
        sections.append(_build_table(top_picks))
    else:
        sections.append("_No listings scored 8 or above this run._")
    sections.append("")

    # Rest
    sections.append("## All verified gigs  (score 6-7)\n")
    if rest:
        sections.append(_build_table(rest))
    else:
        sections.append("_No listings in this range._")
    sections.append("")

    # Run stats
    sections.append("## Run stats\n")
    sections.append("| Stage | Count |")
    sections.append("|---|---|")
    sections.append(f'| Raw scraped | {stats["raw_count"]} |')
    sections.append(f'| After analyst filter | {stats["analyzed_count"]} |')
    sections.append(f'| Dead / expired removed | {stats["dead_count"]} |')
    sections.append(f'| Duplicates removed | {stats["dup_count"]} |')
    sections.append(f'| Final verified | {stats["final_count"]} |')
    sections.append("")

    return "\n".join(sections)


def _serialize_listings(listings: list[ValidatedListing]) -> list[dict]:
    """Convert listings to plain dicts, making datetimes JSON-serialisable."""
    out = []
    for l in listings:
        d = asdict(l)
        for key, val in d.items():
            if isinstance(val, datetime):
                d[key] = val.isoformat()
        out.append(d)
    return out


def write_outputs(listings: list[ValidatedListing], stats: dict) -> None:
    """Write Markdown and JSON output files, then copy latest JSON to docs/."""
    today = date.today().isoformat()

    # --- Markdown ---
    report = format_report(listings, stats)

    md_dated = f"output/gigs_{today}.md"
    md_latest = "output/gigs_latest.md"
    for path in (md_dated, md_latest):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)

    # --- JSON ---
    payload = _serialize_listings(listings)

    json_dated = f"output/gigs_{today}.json"
    json_latest = "output/gigs_latest.json"
    for path in (json_dated, json_latest):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)

    # --- Copy latest JSON to docs/ ---
    shutil.copy2(json_latest, "docs/gigs_latest.json")

    console.print("\n[bold green]Output files written:[/bold green]")
    console.print(f"  [cyan]{md_dated}")
    console.print(f"  [cyan]{md_latest}")
    console.print(f"  [cyan]{json_dated}")
    console.print(f"  [cyan]{json_latest}")
    console.print(f"  [cyan]docs/gigs_latest.json")
