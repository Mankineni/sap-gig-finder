"""Tests for pipeline mechanics — sentinel propagation and queue backpressure."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.models import RawListing
from agents.scout import SENTINEL as SCOUT_SENTINEL
from agents.analyst import run_analysts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw(title="Test Job", index=0) -> RawListing:
    return RawListing(
        title=f"{title} #{index}",
        url=f"https://example.com/job/{index}",
        raw_description="SAP BW/4HANA and Datasphere migration project.",
        source="freelancermap",
    )


def _mock_client_high_score() -> AsyncMock:
    """Return a mock client that always returns a high-score JSON response."""
    payload = json.dumps({
        "workload_days": 2,
        "remote_pct": 100,
        "is_agency": False,
        "tech_stack": ["BW/4HANA", "Datasphere"],
        "score": 9,
        "score_reason": "Great fit",
    })
    content_block = MagicMock()
    content_block.text = payload

    response = MagicMock()
    response.content = [content_block]

    client = AsyncMock()
    client.messages = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSentinelPropagation:
    @pytest.mark.asyncio
    async def test_sentinel_propagation(self):
        """Put 3 listings + SCOUT_SENTINEL into raw_queue.

        After run_analysts finishes, analyzed_queue should contain the 3
        analyzed results followed by the analyst sentinel.
        """
        raw_queue = asyncio.Queue()
        analyzed_queue = asyncio.Queue()
        semaphore = asyncio.Semaphore(4)
        analyst_sentinel = object()

        for i in range(3):
            await raw_queue.put(_make_raw(index=i))
        await raw_queue.put(SCOUT_SENTINEL)

        with patch("agents.analyst.anthropic.AsyncAnthropic") as MockAnthropic:
            MockAnthropic.return_value = _mock_client_high_score()

            await run_analysts(
                raw_queue, analyzed_queue, semaphore,
                SCOUT_SENTINEL, analyst_sentinel,
            )

        # Drain the analyzed_queue.
        items = []
        while not analyzed_queue.empty():
            items.append(analyzed_queue.get_nowait())

        # 3 analyzed listings + 1 sentinel
        assert len(items) == 4
        # Last item must be the analyst sentinel.
        assert items[-1] is analyst_sentinel
        # First 3 should be AnalyzedListing objects with score > 0.
        for item in items[:3]:
            assert hasattr(item, "score")
            assert item.score > 0


class TestQueueBackpressure:
    @pytest.mark.asyncio
    async def test_queue_backpressure(self):
        """A Queue(maxsize=2) should block on the 3rd put when no consumer drains it."""
        q: asyncio.Queue = asyncio.Queue(maxsize=2)

        await q.put("a")
        await q.put("b")

        # The third put should block because the queue is full.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.put("c"), timeout=0.1)
