"""Tests for agents.analyst — Claude API mocking for analysis."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.models import RawListing
from agents.analyst import analyze_one, run_analysts
from config.settings import MIN_SCORE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw(title="SAP BW Consultant", score_hint=8) -> RawListing:
    return RawListing(
        title=title,
        url="https://example.com/job/1",
        raw_description="Looking for SAP BW/4HANA and Datasphere expert.",
        source="freelancermap",
    )


def _mock_client_with_response(text: str) -> AsyncMock:
    """Return a mock AsyncAnthropic whose messages.create returns *text*."""
    content_block = MagicMock()
    content_block.text = text

    response = MagicMock()
    response.content = [content_block]

    client = AsyncMock()
    client.messages = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAnalyzeOne:
    @pytest.mark.asyncio
    async def test_valid_json_response(self):
        payload = {
            "workload_days": 3,
            "remote_pct": 100,
            "is_agency": False,
            "tech_stack": ["SAP BW/4HANA", "Datasphere", "SAC"],
            "score": 9,
            "score_reason": "Modern stack with full remote",
        }
        client = _mock_client_with_response(json.dumps(payload))
        semaphore = asyncio.Semaphore(1)
        listing = _make_raw()

        result = await analyze_one(listing, client, semaphore)

        assert result.workload_days == 3
        assert result.remote_pct == 100
        assert result.is_agency is False
        assert result.tech_stack == ["SAP BW/4HANA", "Datasphere", "SAC"]
        assert result.score == 9
        assert result.score_reason == "Modern stack with full remote"
        # Inherited fields preserved
        assert result.title == listing.title
        assert result.url == listing.url
        assert result.source == listing.source

    @pytest.mark.asyncio
    async def test_invalid_json_response(self):
        client = _mock_client_with_response("This is not JSON at all!!!")
        semaphore = asyncio.Semaphore(1)
        listing = _make_raw()

        result = await analyze_one(listing, client, semaphore)

        assert result.score == 0
        assert result.score_reason == "parse error"

    @pytest.mark.asyncio
    async def test_score_filtered(self):
        """Listings below MIN_SCORE should not appear in analyzed_queue."""
        low_score_payload = json.dumps({
            "workload_days": 5,
            "remote_pct": 0,
            "is_agency": True,
            "tech_stack": ["SAP Basis"],
            "score": 3,
            "score_reason": "Generic support role",
        })

        raw_queue = asyncio.Queue()
        analyzed_queue = asyncio.Queue()
        semaphore = asyncio.Semaphore(4)
        scout_sentinel = object()
        analyst_sentinel = object()

        listing = _make_raw(title="SAP Basis Support")
        await raw_queue.put(listing)
        await raw_queue.put(scout_sentinel)

        with patch("agents.analyst.anthropic.AsyncAnthropic") as MockAnthropic:
            mock_client = _mock_client_with_response(low_score_payload)
            MockAnthropic.return_value = mock_client

            await run_analysts(
                raw_queue, analyzed_queue, semaphore,
                scout_sentinel, analyst_sentinel,
            )

        # The only item in analyzed_queue should be the analyst sentinel,
        # not the low-scoring listing.
        item = analyzed_queue.get_nowait()
        assert item is analyst_sentinel
        assert analyzed_queue.empty()
