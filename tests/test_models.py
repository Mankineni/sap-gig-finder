"""Tests for agents.models — dataclass instantiation and inheritance."""

from datetime import datetime

from agents.models import AnalyzedListing, RawListing, ValidatedListing


class TestRawListing:
    def test_defaults(self):
        listing = RawListing(
            title="SAP BW Consultant",
            url="https://example.com/job/1",
            raw_description="Looking for SAP BW expert.",
            source="gulp",
        )
        assert listing.title == "SAP BW Consultant"
        assert listing.source == "gulp"
        assert isinstance(listing.scraped_at, datetime)

    def test_custom_scraped_at(self):
        ts = datetime(2025, 1, 1)
        listing = RawListing(
            title="Job", url="https://x.com", raw_description="desc",
            source="eursap", scraped_at=ts,
        )
        assert listing.scraped_at == ts


class TestAnalyzedListing:
    def test_defaults(self):
        listing = AnalyzedListing(
            title="SAP ABAP Dev",
            url="https://example.com/job/2",
            raw_description="ABAP role",
            source="freelancermap",
        )
        assert listing.workload_days is None
        assert listing.remote_pct == 0
        assert listing.is_agency is True
        assert listing.tech_stack == []
        assert listing.score == 0
        assert listing.score_reason == ""

    def test_inherits_raw_fields(self):
        listing = AnalyzedListing(
            title="Job", url="https://x.com", raw_description="desc",
            source="gulp", score=8,
        )
        assert hasattr(listing, "title")
        assert hasattr(listing, "url")
        assert hasattr(listing, "raw_description")
        assert hasattr(listing, "source")
        assert hasattr(listing, "scraped_at")
        assert listing.score == 8


class TestValidatedListing:
    def test_defaults(self):
        listing = ValidatedListing(
            title="SAP Datasphere",
            url="https://example.com/job/3",
            raw_description="Datasphere migration",
            source="linkedin",
        )
        assert listing.status == "unknown"
        assert listing.http_code is None
        assert isinstance(listing.verified_at, datetime)

    def test_inherits_analyzed_fields(self):
        listing = ValidatedListing(
            title="Job", url="https://x.com", raw_description="desc",
            source="upwork", score=7, tech_stack=["SAC", "BW"],
            status="live", http_code=200,
        )
        assert listing.score == 7
        assert listing.tech_stack == ["SAC", "BW"]
        assert listing.status == "live"
        assert listing.http_code == 200
        # Fields from RawListing
        assert hasattr(listing, "scraped_at")
        # Fields from AnalyzedListing
        assert hasattr(listing, "workload_days")
        assert hasattr(listing, "remote_pct")
        assert hasattr(listing, "is_agency")
        assert hasattr(listing, "score_reason")
