import sqlite3, json, os

DB_PATH = os.environ.get("DB_PATH", "data/jobpilot.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cvs(id INTEGER PRIMARY KEY, name TEXT, role_tag TEXT, path TEXT, text TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, source TEXT, external_id TEXT UNIQUE, title TEXT, company TEXT,
  location TEXT, url TEXT, description TEXT, apply_email TEXT, apply_type TEXT, score INTEGER, cv_id INTEGER,
  reason TEXT, status TEXT DEFAULT 'new', created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY, job_id INTEGER, cv_id INTEGER, method TEXT, status TEXT,
  subject TEXT, body TEXT, note TEXT, reply_status TEXT, followup_sent INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now','localtime')), applied_at TEXT);
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), level TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
"""

DEFAULT_SETTINGS = {
    "roles": ["Data Analyst"], "locations": ["Delhi", "Remote"],
    "exclude_keywords": ["intern"], "blacklist_companies": [],
    "daily_cap": 50, "min_score": 70, "mode": "digest",
    "send_interval_minutes": 12, "send_window_start": 9, "send_window_end": 21,
    "collect_hour": 8, "followups_enabled": True, "followup_days": 6,
    "greenhouse_boards": [], "lever_boards": [], "max_match_per_run": 120,
    "sources": {"remotive": True, "remoteok": True, "hn": True, "adzuna": False, "greenhouse": True, "lever": True},
    "form_headless": True, "form_dry_run": False,
}
DEFAULT_PROFILE = {k: "" for k in [
    "full_name", "email", "phone", "location", "linkedin", "github", "current_title", "current_company",
    "years_experience", "current_ctc", "expected_ctc", "notice_period", "work_authorization",
    "willing_to_relocate", "skills", "summary", "standard_answers", "signature"]}


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def q(sql, args=(), one=False):
    c = conn()
    try:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()
    if one:
        return rows[0] if rows else None
    return rows


def x(sql, args=()):
    c = conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def init():
    c = conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def get_kv(key, default):
    r = q("SELECT value FROM kv WHERE key=?", (key,), one=True)
    return json.loads(r["value"]) if r else default


def set_kv(key, value):
    x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def get_settings():
    s = {**DEFAULT_SETTINGS, **get_kv("settings", {})}
    s["sources"] = {**DEFAULT_SETTINGS["sources"], **s.get("sources", {})}
    return s


def get_profile():
    return {**DEFAULT_PROFILE, **get_kv("profile", {})}
