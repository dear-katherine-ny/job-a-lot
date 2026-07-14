"""Quick script to verify which ATS slugs are valid."""

import csv
import sys
from pathlib import Path

import requests

COMPANIES_CSV = Path(__file__).parent / "companies.csv"


def check_greenhouse(slug: str) -> bool:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = requests.get(url, timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def check_lever(slug: str) -> bool:
    url = f"https://api.lever.co/v0/postings/{slug}"
    try:
        resp = requests.get(url, timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def main():
    with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        companies = list(reader)

    ok = 0
    fail = 0
    skip = 0

    for c in companies:
        name = c["name"]
        ats = c["ats"]
        slug = c["slug"]

        if ats == "google" or not slug:
            print(f"  SKIP  {name} (google search)")
            skip += 1
            continue

        if ats == "greenhouse":
            valid = check_greenhouse(slug)
        elif ats == "lever":
            valid = check_lever(slug)
        else:
            print(f"  SKIP  {name} (unknown ats: {ats})")
            skip += 1
            continue

        status = "OK" if valid else "FAIL"
        icon = "✅" if valid else "❌"
        print(f"  {icon} {status:4s}  {name} ({ats}/{slug})")

        if valid:
            ok += 1
        else:
            fail += 1

    print(f"\n--- Summary ---")
    print(f"  OK: {ok}  |  FAIL: {fail}  |  SKIP: {skip}  |  TOTAL: {len(companies)}")


if __name__ == "__main__":
    main()
