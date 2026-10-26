"""Job sources. Only public APIs / boards that permit programmatic access (no LinkedIn / Naukri scraping)."""
import re, os, html, httpx

UA = {"User-Agent": "JobPilot/1.0 (personal job search tool)"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(r"^(no-?reply|donotreply)@|@(example|sentry)\.", re.I)
FORM_HOSTS = ("greenhouse.io", "lever.co", "docs.google.com/forms", "forms.gle", "workable.com", "ashbyhq.com")


def get(url, **params):
    r = httpx.get(url, params=params or None, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<(p|br|li|/p|/li|/div)[^>]*>", "\n", s)
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def find_email(text):
    for m in EMAIL_RE.findall(text or ""):
        if not BAD_EMAIL.search(m):
            return m
    return None


def classify(url, email):
    if email:
        return "email"
    if url and any(h in url for h in FORM_HOSTS):
        return "form"
    return "manual"


def mk(source, ext, title, company, location, url, desc, email=None):
    email = email or find_email(desc)
    return {"source": source, "external_id": f"{source}-{ext}", "title": (title or "")[:200], "company": (company or "")[:120],
            "location": (location or "")[:120], "url": url, "description": (desc or "")[:8000],
            "apply_email": email, "apply_type": classify(url, email)}


def role_match(text, roles):
    if not roles:
        return True
    t = (text or "").lower()
    for r in roles:
        words = [w for w in re.findall(r"[a-z0-9+#.]+", r.lower()) if len(w) > 2]
        if words and all(w in t for w in words):
            return True
    return False


def remotive(s):
    out = []
    for role in s["roles"] or [""]:
        for j in get("https://remotive.com/api/remote-jobs", search=role).get("jobs", []):
            out.append(mk("remotive", j["id"], j["title"], j["company_name"], j.get("candidate_required_location"),
                          j["url"], strip_html(j.get("description"))))
    return out


def remoteok(s):
    data = get("https://remoteok.com/api")
    out = []
    for j in data[1:]:
        text = f'{j.get("position","")} {" ".join(j.get("tags", []))}'
        if role_match(text, s["roles"]):
            out.append(mk("remoteok", j["id"], j.get("position"), j.get("company"), j.get("location") or "Remote",
                          j.get("apply_url") or j.get("url"), strip_html(j.get("description"))))
    return out


def hn(s):
    hits = get("https://hn.algolia.com/api/v1/search_by_date", query="who is hiring", tags="story,author_whoishiring",
               hitsPerPage=5).get("hits", [])
    thread = next((h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None)
    if not thread:
        return []
    out = []
    for c in get(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}").get("children", []):
        text = strip_html(c.get("text"))
        if not text or not role_match(text, s["roles"]):
            continue
        first = text.split("\n")[0]
        company = first.split("|")[0].strip()
        out.append(mk("hn", c["id"], first[:160], company, "", f"https://news.ycombinator.com/item?id={c['id']}", text))
    return out


def adzuna(s):
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
    country, out = os.environ.get("ADZUNA_COUNTRY", "in"), []
    for role in s["roles"]:
        for loc in (s["locations"] or [""]):
            if loc.lower() == "remote":
                loc = ""
            res = get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", app_id=app_id, app_key=key,
                      what=role, where=loc, results_per_page=50).get("results", [])
            for j in res:
                out.append(mk("adzuna", j["id"], j["title"], j.get("company", {}).get("display_name"),
                              j.get("location", {}).get("display_name"), j["redirect_url"], j.get("description")))
    return out


def greenhouse(s):
    out = []
    for slug in s["greenhouse_boards"]:
        try:
            jobs = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", content="true").get("jobs", [])
        except Exception:
            continue
        for j in jobs:
            if role_match(j["title"], s["roles"]):
                out.append(mk("greenhouse", j["id"], j["title"], slug, (j.get("location") or {}).get("name"),
                              j["absolute_url"], strip_html(j.get("content"))))
    return out


def lever(s):
    out = []
    for slug in s["lever_boards"]:
        try:
            jobs = get(f"https://api.lever.co/v0/postings/{slug}", mode="json")
        except Exception:
            continue
        for j in jobs:
            if role_match(j["text"], s["roles"]):
                out.append(mk("lever", j["id"], j["text"], slug, (j.get("categories") or {}).get("location"),
                              j.get("applyUrl") or j["hostedUrl"], j.get("descriptionPlain")))
    return out


REGISTRY = {"remotive": remotive, "remoteok": remoteok, "hn": hn, "adzuna": adzuna, "greenhouse": greenhouse, "lever": lever}
