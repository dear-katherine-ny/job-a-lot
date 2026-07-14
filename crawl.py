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
                jobs.append({
                    "title": title,
                    "url": job.get("hostedUrl", ""),
                    "location": location,
                    "posted": "",
                })
        return jobs
    except requests.RequestException:
        return []


def crawl_google(company_name: str, career_url: str) -> list[dict]:
    """Search for job postings via Google Custom Search API."""
    api_key = os.getenv("GOOGLE_CSE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return []

    # Build query with title keywords
    keyword_query = " OR ".join(f'"{kw}"' for kw in TITLE_KEYWORDS[:5])
    query = f'{company_name} ({keyword_query})'

    url = "https://www.googleapis.com/customsearch/v1"
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
            return []
        data = resp.json()
        jobs = []
        for item in data.get("items", []):
            title = item.get("title", "")
            link = item.get("link", "")
            if _matches_title(title) and _is_job_url(link):
                jobs.append({
                    "title": title,
                    "url": link,
                    "location": "",
                    "posted": "",
                })
        return jobs
    except requests.RequestException:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    ]
    return any(ind in url.lower() for ind in job_indicators)


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


def send_notification(new_jobs: list[dict]):
    """Send email notification for new job postings."""
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    if not smtp_email or not smtp_password or not new_jobs:
        return

    subject = f"[Job Crawler] {len(new_jobs)} new posting(s) found - {datetime.now().strftime('%Y-%m-%d')}"

    body_lines = [f"Found {len(new_jobs)} new job posting(s):\n"]
    for job in new_jobs:
        body_lines.append(f"🏢 {job['company']} (Priority: {job['priority']})")
        body_lines.append(f"   {job['title']}")
        body_lines.append(f"   📍 {job['location']}")
        body_lines.append(f"   🔗 {job['url']}")
        body_lines.append("")

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

    for company in companies:
        name = company["name"]
        ats = company["ats"]
        slug = company["slug"]
        career_url = company["career_url"]
        priority = company["priority"]

        # Crawl based on ATS type
        if ats == "greenhouse" and slug:
            jobs = crawl_greenhouse(slug)
            source = "Greenhouse"
        elif ats == "lever" and slug:
            jobs = crawl_lever(slug)
            source = "Lever"
        elif ats == "google":
            jobs = crawl_google(name, career_url)
            source = "Google Search"
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

    print(f"\n--- Results ---")
    print(f"Total matching jobs found: {total_found}")
    print(f"New (not in sheet): {len(all_new_jobs)}")

    if all_new_jobs:
        append_jobs(worksheet, all_new_jobs)
        print(f"Appended {len(all_new_jobs)} rows to Google Sheets")

        send_notification(all_new_jobs)
    else:
        print("No new postings today.")

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
