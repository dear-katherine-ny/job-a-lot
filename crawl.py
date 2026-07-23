"""
Job Crawler - Daily job posting monitor for target companies.
Checks Greenhouse, Lever, and Google Custom Search APIs.
Results go to Google Sheets with email notifications for new postings.
"""

import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TITLE_KEYWORDS = [
    "technical writer",
    "documentation engineer",
    "documentation writer",
    "technical author",
    "documentation specialist",
    "developer documentation",
    "api documentation",
    "information developer",
    "developer experience",
    "knowledge engineer",
    "ux writer",
    "content strategist",
    "docs engineer",
    "documentation manager",
    "technical documentation",
]

COMPANIES_CSV = Path(__file__).parent / "companies.csv"

# ---------------------------------------------------------------------------
# Crawlers
# ---------------------------------------------------------------------------


def crawl_greenhouse(slug: str) -> list[dict]:
    """Fetch jobs from Greenhouse public job board API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        jobs = []
        for job in data.get("jobs", []):
            title = job.get("title", "")
            if _matches_title(title):
                location = _extract_greenhouse_location(job)
                if _is_excluded_location(location):
                    continue
                jobs.append({
                    "title": title,
                    "url": job.get("absolute_url", ""),
                    "location": location,
                    "posted": job.get("updated_at", "")[:10],
                })
        return jobs
    except requests.RequestException:
        return []


def crawl_lever(slug: str) -> list[dict]:
    """Fetch jobs from Lever public postings API."""
    url = f"https://api.lever.co/v0/postings/{slug}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        jobs = []
        for job in data:
            title = job.get("text", "")
            if _matches_title(title):
                location = job.get("categories", {}).get("location", "")
                if _is_excluded_location(location):
                    continue
                jobs.append({
                    "title": title,
                    "url": job.get("hostedUrl", ""),
                    "location": location,
                    "posted": "",
                })
        return jobs
    except requests.RequestException:
        return []


def crawl_google_consolidated() -> list[dict]:
    """Search for job postings via Google Custom Search API using consolidated queries.

    Instead of one API call per company, runs a few keyword-based queries
    across job platforms to stay within the free 100 queries/day limit.
    """
    api_key = os.getenv("GOOGLE_CSE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return []

    # Consolidated queries — each covers many companies at once
    queries = [
        '"technical writer" OR "documentation engineer" OR "documentation writer"',
        '"technical author" OR "docs engineer" OR "documentation specialist"',
        '"developer documentation" OR "api documentation" OR "documentation manager"',
        '"technical writer" remote Australia',
        '"documentation engineer" remote Europe',
    ]

    url = "https://www.googleapis.com/customsearch/v1"
    all_jobs = []
    seen_urls = set()

    for query in queries:
        params = {
            "key": api_key,
            "cx": cse_id,
            "q": query,
            "num": 10,
            "dateRestrict": "w1",  # Last week
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in data.get("items", []):
                title = item.get("title", "")
                link = item.get("link", "")
                if link in seen_urls:
                    continue
                if _matches_title(title) and _is_job_url(link):
                    company = _extract_company_from_url(link)
                    all_jobs.append({
                        "title": title,
                        "url": link,
                        "location": "",
                        "posted": "",
                        "company": company,
                    })
                    seen_urls.add(link)
        except requests.RequestException:
            continue

    return all_jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


EXCLUDED_LOCATIONS = [
    "india", "bangalore", "bengaluru", "hyderabad", "mumbai",
    "pune", "chennai", "delhi", "noida", "gurgaon", "gurugram",
    "kolkata", "ahmedabad",
]


def _is_excluded_location(location: str) -> bool:
    """Check if a job location is in an excluded region."""
    location_lower = location.lower()
    return any(loc in location_lower for loc in EXCLUDED_LOCATIONS)


def _matches_title(title: str) -> bool:
    """Check if a job title matches any of our target keywords."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in TITLE_KEYWORDS)


def _extract_greenhouse_location(job: dict) -> str:
    """Extract location string from Greenhouse job data."""
    locations = job.get("location", {}).get("name", "")
    return locations


def _is_job_url(url: str) -> bool:
    """Basic check that a URL looks like a job posting."""
    job_indicators = [
        "jobs", "careers", "greenhouse", "lever",
        "workday", "linkedin.com/jobs", "posting",
        "indeed.com", "glassdoor.com", "seek.com.au",
    ]
    return any(ind in url.lower() for ind in job_indicators)


def _extract_company_from_url(url: str) -> str:
    """Best-effort company name extraction from a job posting URL."""
    url_lower = url.lower()
    # Known job board patterns
    if "greenhouse.io" in url_lower:
        # https://boards.greenhouse.io/companyname/...
        parts = url.split("/")
        for i, p in enumerate(parts):
            if "greenhouse" in p.lower() and i + 1 < len(parts):
                return parts[i + 1].replace("-", " ").title()
    if "lever.co" in url_lower:
        # https://jobs.lever.co/companyname/...
        parts = url.split("/")
        for i, p in enumerate(parts):
            if "lever" in p.lower() and i + 1 < len(parts):
                return parts[i + 1].replace("-", " ").title()
    if "linkedin.com" in url_lower:
        return "LinkedIn Posting"
    if "indeed.com" in url_lower:
        return "Indeed Posting"
    if "seek.com" in url_lower:
        return "Seek Posting"
    # Fallback: use domain name
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.replace("www.", "")
        return domain.split(".")[0].title()
    except Exception:
        return "Unknown"


def load_companies() -> list[dict]:
    """Load target companies from CSV."""
    companies = []
    with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            companies.append(row)
    return companies


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


def get_sheet():
    """Connect to Google Sheets and return the worksheet."""
    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not creds_path or not spreadsheet_id:
        return None

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet("Job Postings")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet("Job Postings", rows=1000, cols=10)
        worksheet.append_row([
            "Date Found", "Company", "Title", "Location",
            "URL", "Source", "Priority", "Status",
        ])

    return worksheet


def get_existing_urls(worksheet) -> set[str]:
    """Get all URLs already in the sheet to avoid duplicates."""
    if worksheet is None:
        return set()
    try:
        urls = worksheet.col_values(5)  # URL column (E)
        return set(urls[1:])  # Skip header
    except Exception:
        return set()


def append_jobs(worksheet, jobs: list[dict]):
    """Append new job rows to the sheet."""
    if worksheet is None or not jobs:
        return
    rows = []
    for job in jobs:
        rows.append([
            job["date_found"],
            job["company"],
            job["title"],
            job["location"],
            job["url"],
            job["source"],
            job["priority"],
            "New",
        ])
    worksheet.append_rows(rows)


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------


def send_notification(new_jobs: list[dict], worksheet=None):
    """Send email notification for job crawl results (including zero results).

    Checks the 'Metadata' sheet for the last email date to prevent
    duplicate sends on the same day.
    """
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    if not smtp_email or not smtp_password:
        return

    today = datetime.now().strftime("%Y-%m-%d")

    # Dedup: skip if already sent today
    if worksheet is not None:
        try:
            spreadsheet = worksheet.spreadsheet
            try:
                meta = spreadsheet.worksheet("Metadata")
            except gspread.WorksheetNotFound:
                meta = spreadsheet.add_worksheet("Metadata", rows=10, cols=2)
                meta.update_cell(1, 1, "last_email_date")
                meta.update_cell(1, 2, "")

            last_date = meta.cell(1, 2).value
            if last_date == today:
                print(f"Email already sent today ({today}) — skipping")
                return
        except Exception as e:
            print(f"Metadata check failed ({e}) — proceeding with send")

    if new_jobs:
        subject = f"[Job Crawler] {len(new_jobs)} new posting(s) found - {today}"
        body_lines = [f"Found {len(new_jobs)} new job posting(s):\n"]
        for job in new_jobs:
            priority_str = f" (Priority: {job['priority']})" if job['priority'] else ""
            body_lines.append(f"🏢 {job['company']}{priority_str}")
            body_lines.append(f"   {job['title']}")
            if job['location']:
                body_lines.append(f"   📍 {job['location']}")
            body_lines.append(f"   🔗 {job['url']}")
            body_lines.append("")
    else:
        subject = f"[Job Crawler] No new postings - {today}"
        body_lines = [
            "No new job postings found today.",
            "",
            "Crawler ran successfully — all results were either duplicates or no matches.",
        ]

    body = "\n".join(body_lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = smtp_email

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, smtp_email, msg.as_string())
        print(f"Email sent: {len(new_jobs)} new postings")

        # Record send date to prevent duplicate sends
        if worksheet is not None:
            try:
                meta = worksheet.spreadsheet.worksheet("Metadata")
                meta.update_cell(1, 2, today)
            except Exception:
                pass
    except Exception as e:
        print(f"Email failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print(f"=== Job Crawler - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    companies = load_companies()
    print(f"Loaded {len(companies)} companies\n")

    worksheet = get_sheet()
    existing_urls = get_existing_urls(worksheet)
    print(f"Existing postings in sheet: {len(existing_urls)}\n")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_new_jobs = []
    total_found = 0

    # Phase 1: Direct API crawling (Greenhouse/Lever — free, unlimited)
    print("--- Phase 1: Direct API (Greenhouse/Lever) ---")
    for company in companies:
        name = company["name"]
        ats = company["ats"]
        slug = company["slug"]
        priority = company["priority"]

        if ats == "greenhouse" and slug:
            jobs = crawl_greenhouse(slug)
            source = "Greenhouse"
        elif ats == "lever" and slug:
            jobs = crawl_lever(slug)
            source = "Lever"
        else:
            continue

        if jobs:
            total_found += len(jobs)
            print(f"  {name}: {len(jobs)} matching job(s) [{source}]")

        for job in jobs:
            if job["url"] and job["url"] not in existing_urls:
                all_new_jobs.append({
                    "date_found": today,
                    "company": name,
                    "title": job["title"],
                    "location": job["location"],
                    "url": job["url"],
                    "source": source,
                    "priority": priority,
                })
                existing_urls.add(job["url"])

    # Phase 2: Consolidated Google Search (5 queries instead of 50+)
    print("\n--- Phase 2: Consolidated Google Search ---")
    google_jobs = crawl_google_consolidated()
    if google_jobs:
        total_found += len(google_jobs)
        print(f"  Google Search: {len(google_jobs)} matching job(s)")

    for job in google_jobs:
        if job["url"] and job["url"] not in existing_urls:
            all_new_jobs.append({
                "date_found": today,
                "company": job.get("company", "Unknown"),
                "title": job["title"],
                "location": job["location"],
                "url": job["url"],
                "source": "Google Search",
                "priority": "",
            })
            existing_urls.add(job["url"])

    print(f"\n--- Results ---")
    print(f"Total matching jobs found: {total_found}")
    print(f"New (not in sheet): {len(all_new_jobs)}")

    if all_new_jobs:
        append_jobs(worksheet, all_new_jobs)
        print(f"Appended {len(all_new_jobs)} rows to Google Sheets")

    # Always send notification (including zero results)
    send_notification(all_new_jobs, worksheet)

    # Console summary for CI logs
    if all_new_jobs:
        print("\n--- New Postings ---")
        for job in all_new_jobs:
            print(f"  [{job['priority']}] {job['company']} - {job['title']}")
            print(f"      {job['url']}")

    return len(all_new_jobs)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
