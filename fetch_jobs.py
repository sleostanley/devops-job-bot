"""
Phase 1 + 2: Fetch DevOps jobs -> dedupe -> filter -> Gemini match score + cover letter -> log.
Logs to Google Sheets if GOOGLE_CREDS_JSON + SHEET_ID are set, else to jobs.csv.
Scoring is skipped if GEMINI_API_KEY is not set.
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

# Empty list = accept all. "worldwide"/"anywhere"/"global" always pass when list is non-empty.
ALLOWED_LOCATIONS = [
    "india", "chennai", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
    "delhi", "gurgaon", "gurugram", "noida", "kochi", "coimbatore",
    "uae", "united arab emirates", "dubai", "abu dhabi",
    "japan", "tokyo", "osaka", "asia", "apac",
]

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SCORE_THRESHOLD = 70        # score >= this -> status "ready" + cover letter
MAX_SCORE_PER_RUN = 40      # free-tier safety cap per daily run
SLEEP_BETWEEN_CALLS = 7     # seconds (keeps under ~10 requests/min)
RESUME_PATH = "resume_profile.txt"

HEADERS = {"User-Agent": "Mozilla/5.0 (devops-job-bot; personal use)"}
TIMEOUT = 25
CSV_PATH = "jobs.csv"
COLUMNS = ["date_added", "source", "title", "company", "location", "salary",
           "posted", "url", "score", "reason", "cover_letter", "status"]
# -----------------------------------------------------


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def job(source, title, company, location, url, posted="", salary="", description=""):
    return {"source": source, "title": clean(title), "company": clean(company),
            "location": clean(location) or "Not specified", "url": (url or "").strip(),
            "posted": str(posted or ""), "salary": str(salary or ""),
            "description": clean(description)[:3500],
            "score": "", "reason": "", "cover_letter": ""}


def ts(v):
    """Unix timestamp -> YYYY-MM-DD (falls back to raw string)."""
    try:
        return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(v or "")


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------- SOURCES ----------------
def fetch_remotive():
    data = get_json("https://remotive.com/api/remote-jobs?category=devops")
    return [job("remotive", j["title"], j["company_name"],
                j.get("candidate_required_location"), j["url"],
                j.get("publication_date"), j.get("salary"), j.get("description"))
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
                       j.get("date"), sal, j.get("description")))
    return out


def fetch_arbeitnow():
    out = []
    for page in (1, 2, 3):
        data = get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        for j in data.get("data", []):
            if not j.get("remote"):  # remote jobs only
                continue
            loc = f"Remote / {j.get('location', '')}"
            out.append(job("arbeitnow", j["title"], j["company_name"], loc,
                           j["url"], ts(j.get("created_at")), "", j.get("description")))
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
                       e.get("published"), "", e.get("summary")))
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


# ---------------- GEMINI MATCHING ----------------
PROMPT = """You are a strict, honest recruiter-screening assistant.

CANDIDATE PROFILE:
{profile}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description: {description}

Score how well this candidate fits this job from 0 to 100. Rules:
- The candidate has about 1 year of professional experience. If the job requires 4+ years, or is a Senior/Lead/Staff/Principal/Manager role without saying 1-3 years is fine, the score must be 45 or lower.
- Penalize missing core required skills. Reward matching tools (Kubernetes, Terraform, AWS, GitHub Actions, Docker, Prometheus/Grafana, Vault, etc).
- If the job is on-site/hybrid outside India, UAE, Japan, or requires work authorization the candidate likely lacks (e.g. US/EU-only), score 40 or lower.
- Remote worldwide roles open to India-based candidates are fine.

If score >= {threshold}, also write a cover letter: max 170 words, addressed to "Hiring Team", signed "Leo Stanley S". Use ONLY facts from the candidate profile. Never invent years of experience, employers, tools or numbers. Mention 2-3 of the most relevant achievements. Plain text, no placeholders. If score < {threshold}, cover_letter must be an empty string.

Respond with ONLY JSON: {{"score": <int>, "reason": "<one short sentence>", "cover_letter": "<text or empty>"}}"""


def call_gemini(prompt, api_key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}}
    for attempt in range(4):
        r = requests.post(url, headers={"x-goog-api-key": api_key,
                                        "Content-Type": "application/json"},
                          json=body, timeout=90)
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    raise RuntimeError("Gemini rate limit (429) after retries")


def score_job(profile, j, api_key):
    prompt = PROMPT.format(profile=profile, title=j["title"], company=j["company"],
                           location=j["location"], description=j["description"] or "(no description)",
                           threshold=SCORE_THRESHOLD)
    res = call_gemini(prompt, api_key)
    score = max(0, min(100, int(res.get("score", 0))))
    letter = (res.get("cover_letter") or "").strip() if score >= SCORE_THRESHOLD else ""
    return {"score": score, "reason": clean(res.get("reason", ""))[:300], "cover_letter": letter}


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
            print("Storage: local jobs.csv")

    def seen_urls(self):
        idx = COLUMNS.index("url")
        if self.ws:
            return set(self.ws.col_values(idx + 1))
        if not os.path.exists(CSV_PATH):
            return set()
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            return {row.get("url") for row in csv.DictReader(f)}

    def add(self, jobs):
        today = dt.date.today().isoformat()
        rows = []
        for j in jobs:
            if j["score"] == "":
                status = "new"
            else:
                status = "ready" if j["score"] >= SCORE_THRESHOLD else "skip"
            rows.append([today, j["source"], j["title"], j["company"], j["location"],
                         j["salary"], j["posted"], j["url"], j["score"], j["reason"],
                         j["cover_letter"], status])
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
            hit = sum(1 for j in res if j["url"] and title_ok(j) and location_ok(j))
            print(f"{src.__name__}: fetched {len(res)}, matched {hit}")
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

    api_key = os.environ.get("GEMINI_API_KEY")
    to_log = fresh
    if api_key and fresh and os.path.exists(RESUME_PATH):
        with open(RESUME_PATH, encoding="utf-8") as f:
            profile = f.read()
        to_log, fails = [], 0
        for j in fresh[:MAX_SCORE_PER_RUN]:
            try:
                j.update(score_job(profile, j, api_key))
                to_log.append(j)
                fails = 0
                print(f"  {j['score']:>3}  {j['title']} @ {j['company']}")
            except Exception as e:
                fails += 1
                print(f"  scoring failed ({j['title']}): {type(e).__name__}: {e}", file=sys.stderr)
                if fails >= 3:
                    print("3 failures in a row, stopping scoring for this run.", file=sys.stderr)
                    break
            time.sleep(SLEEP_BETWEEN_CALLS)
        # unscored jobs are NOT logged, so they get retried on the next run
    elif fresh:
        print("GEMINI_API_KEY or resume_profile.txt missing: logging without scores.")

    ready = sum(1 for j in to_log if j["score"] != "" and j["score"] >= SCORE_THRESHOLD)
    print(f"Logging {len(to_log)} jobs ({ready} ready to apply)")
    if to_log:
        store.add(to_log)


if __name__ == "__main__":
    main()
