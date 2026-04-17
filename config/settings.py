from dotenv import load_dotenv
import os

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

SOURCES = {
    "gulp":          {"url": "https://www.gulp.de/gulp2/g/projekte", "method": "playwright"},
    "freelancermap": {"url": "https://www.freelancermap.de/projektboerse.html", "method": "rss"},
    "linkedin":      {"url": "https://rapidapi.com/linkedin", "method": "api"},
    "upwork":        {"url": "https://www.upwork.com/ab/jobs/search/", "method": "api"},
    "eursap":        {"url": "https://eursap.eu/jobs/", "method": "playwright"},
}

SEARCH_QUERIES = [
    '"SAP BW" OR "Datasphere" OR "SAC"',
    '"SAP ABAP" AND ("Optimization" OR "Clean Code")',
    '"SAP" AND ("AI" OR "LLM" OR "Automation")',
]

ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
MIN_SCORE = 6
VALIDATOR_TIMEOUT = 10
MAX_ANALYST_CONCURRENCY = 4
MAX_VALIDATOR_CONCURRENCY = 3
QUEUE_MAX_SIZE = 100
PIPELINE_TIMEOUT_SECONDS = 600

EXPIRED_PHRASES = [
    "Projekt nicht mehr verfügbar",
    "Job expired",
    "Position closed",
    "No longer available",
    "Diese Stelle ist nicht mehr aktiv",
]
