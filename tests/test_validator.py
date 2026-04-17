"""Tests for agents.validator — HTTP checks, deduplication, expiry detection."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from agents.models import AnalyzedListing, ValidatedListing
from agents.validator import check_http, deduplicate, validate_one


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_analyzed(
    title="SAP BW Job",
    url="https://example.com/job/1",
    source="freelancermap",
    score=7,
    **kwargs,
) -> AnalyzedListing:
    return AnalyzedListing(
        title=title,
        url=url,
        raw_description="desc",
        source=source,
        score=score,
        **kwargs,
    )


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("HEAD", "https://x.com"))


# ---------------------------------------------------------------------------
# LAYER 1 — HTTP HEAD check
# ---------------------------------------------------------------------------

class TestCheckHttp:
    @pytest.mark.asyncio
    async def test_live_url(self):
        with patch("agents.validator.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.head.return_value = _mock_response(200)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            status, code = await check_http("https://example.com")
            assert status == "live"
            assert code == 200

    @pytest.mark.asyncio
    async def test_dead_404(self):
        with patch("agents.validator.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.head.return_value = _mock_response(404)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            status, code = await check_http("https://example.com")
            assert status == "dead"
            assert code == 404

    @pytest.mark.asyncio
    async def test_dead_410(self):
        with patch("agents.validator.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.head.return_value = _mock_response(410)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            status, code = await check_http("https://example.com")
            assert status == "dead"
            assert code == 410

    @pytest.mark.asyncio
    async def test_unknown_403(self):
        with patch("agents.validator.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.head.return_value = _mock_response(403)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            status, code = await check_http("https://example.com")
            assert status == "unknown"
            assert code == 403

    @pytest.mark.asyncio
    async def test_network_error(self):
        with patch("agents.validator.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.head.side_effect = httpx.ConnectError("connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            status, code = await check_http("https://example.com")
            assert status == "unknown"
            assert code == 0

    @pytest.mark.asyncio
    async def test_redirect_followed(self):
        """301 followed by 200 — follow_redirects=True means final code is 200."""
        with patch("agents.validator.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # httpx with follow_redirects=True returns the final response.
            mock_client.head.return_value = _mock_response(200)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            status, code = await check_http("https://example.com/old")
            assert status == "live"
            assert code == 200


# ---------------------------------------------------------------------------
# LAYER 2 — Deduplication
# ---------------------------------------------------------------------------

class TestDeduplicate:
    def test_duplicate_detection(self):
        a = ValidatedListing(
            title="SAP BW Job", url="https://example.com/job/1",
            raw_description="desc", source="gulp", score=5, status="live",
        )
        b = ValidatedListing(
            title="SAP BW Job", url="https://example.com/job/1",
            raw_description="desc copy", source="gulp", score=8, status="live",
        )
        result = deduplicate([a, b])
        assert len(result) == 1
        assert result[0].score == 8  # higher score kept


# ---------------------------------------------------------------------------
# LAYER 3 — Playwright expiry check (via validate_one)
# ---------------------------------------------------------------------------

class TestExpiredPhrase:
    @pytest.mark.asyncio
    async def test_expired_phrase(self):
        listing = _make_analyzed(source="gulp", url="https://gulp.de/project/123")

        with patch("agents.validator.check_http", return_value=("live", 200)):
            with patch("agents.validator.check_expired", return_value=True):
                result = await validate_one(listing)

        assert result.status == "expired"
        assert result.http_code == 200
