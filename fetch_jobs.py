"""
Phase 1: Fetch DevOps jobs from free sources -> dedupe -> filter -> log.
Logs to Google Sheets if GOOGLE_CREDS_JSON + SHEET_ID are set, else to jobs.csv.
"""
import csv
import json
import os
import re
import sys
import time
import datetime as dt

import feedparser
import requests

# ---------------- CONFIG (edit these) ----------------
KEYWORDS = [
    "devops", "sre", "site reliability", "platform engineer", "kubernetes",
    "terraform", "cloud engineer", "infrastructure engineer", "aws engineer",
    "ci/cd", "release engineer", "devsecops",
]
EXCLUDE = ["intern", "internship", "sales", "marketing", "recruiter", "designer"]

# Empty list = accept all locations. Example: ["india", "uae", "germany", "singapore", "uk"]
# "worldwide"/"anywhere"/"global" always pass when the list is non-empty.
ALLOWED_LOCATIONS = []

HEADERS = {"User-Agent": "Mozilla/5.0 (devops-job-bot; personal use)"}
TIMEOUT = 25
CSV_PATH = "jobs.csv"
COLUMNS = ["date_added", "source", "title", "company", "location", "salary",
           "posted", "url", "status"]
# -----------------------------------------------------


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def job(source, title, company, location, url, posted="", salary=""):
    return {"source": source, "title": clean(title), "company": clean(company),
            "location": clean(location) or "Not specified", "url": (url or "").strip(),
            "posted": str(posted or ""), "salary": str(salary or "")}


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------- SOURCES ----------------
def fetch_remotive():
    data = get_json("https://remotive.com/api/remote-jobs?category=devops")
    return [job("remotive", j["title"], j["company_name"],
                j.get("candidate_required_location"), j["url"],
                j.get("publication_date"), j.get("salary"))
            for j in data.get("jobs", [])]


def fetch_remoteok():
    data = get_json("https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:
            continue  # first element is a legal notice
        sal = ""
        if j.get("salary_min"):
            sal = f"{j['salary_min']}-{j.get('salary_max', '')}"
        out.append(job("remoteok", j["position"], j.get("company"),
                       j.get("location") or "Worldwide", j.get("url"),
                       j.get("date"), sal))
    return out


def fetch_arbeitnow():
    out = []
    for page in (1, 2, 3):
        data = get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        for j in data.get("data", []):
            loc = j.get("location", "")
            if j.get("remote"):
                loc = f"Remote / {loc}"
            out.append(job("arbeitnow", j["title"], j["company_name"], loc,
                           j["url"], j.get("created_at")))
        time.sleep(1)
    return out


def fetch_wwr():
    feed = feedparser.parse(
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        request_headers=HEADERS)
    out = []
    for e in feed.entries:
        title = e.get("title", "")
        company, _, role = title.partition(": ")
        if not role:
            company, role = "", title
        out.append(job("weworkremotely", role, company,
                       e.get("region", "Worldwide"), e.get("link"),
                       e.get("published")))
    return out


SOURCES = [fetch_remotive, fetch_remoteok, fetch_arbeitnow, fetch_wwr]


# ---------------- FILTERS ----------------
def title_ok(j):
    t = j["title"].lower()
    return any(k in t for k in KEYWORDS) and not any(x in t for x in EXCLUDE)


def location_ok(j):
    if not ALLOWED_LOCATIONS:
        return True
    loc = j["location"].lower()
    if any(w in loc for w in ("worldwide", "anywhere", "global")):
        return True
    return any(a.lower() in loc for a in ALLOWED_LOCATIONS)


# ---------------- STORAGE ----------------
class Store:
    def __init__(self):
        creds = os.environ.get("GOOGLE_CREDS_JSON")
        sheet_id = os.environ.get("SHEET_ID")
        self.ws = None
        if creds and sheet_id:
            import gspread
            gc = gspread.service_account_from_dict(json.loads(creds))
            self.ws = gc.open_by_key(sheet_id).sheet1
            if not self.ws.get_all_values():
                self.ws.append_row(COLUMNS)
            print("Storage: Google Sheets")
        else:
            print("Storage: local jobs.csv (set GOOGLE_CREDS_JSON + SHEET_ID for Sheets)")

    def seen_urls(self):
        idx = COLUMNS.index("url")
        if self.ws:
            return set(self.ws.col_values(idx + 1))
        if not os.path.exists(CSV_PATH):
            return set()
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            return {row["url"] for row in csv.DictReader(f)}

    def add(self, jobs):
        today = dt.date.today().isoformat()
        rows = [[today, j["source"], j["title"], j["company"], j["location"],
                 j["salary"], j["posted"], j["url"], "new"] for j in jobs]
        if self.ws:
            self.ws.append_rows(rows, value_input_option="RAW")
            return
        new_file = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(COLUMNS)
            w.writerows(rows)


# ---------------- MAIN ----------------
def main():
    all_jobs = []
    for src in SOURCES:
        try:
            res = src()
            print(f"{src.__name__}: {len(res)} jobs")
            all_jobs += res
        except Exception as e:  # one dead source shouldn't kill the run
            print(f"{src.__name__} FAILED: {e}", file=sys.stderr)

    matched = [j for j in all_jobs if j["url"] and title_ok(j) and location_ok(j)]
    print(f"After filters: {len(matched)}")

    store = Store()
    seen = store.seen_urls()
    fresh, batch_urls = [], set()
    for j in matched:
        if j["url"] in seen or j["url"] in batch_urls:
            continue
        batch_urls.add(j["url"])
        fresh.append(j)

    print(f"New (not seen before): {len(fresh)}")
    if fresh:
        store.add(fresh)


if __name__ == "__main__":
    main()
