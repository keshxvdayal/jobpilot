import os, json, re
import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
_client = None


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def ask(system, user, max_tokens=1200):
    r = client().messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text")


def ask_json(system, user, max_tokens=1200):
    t = ask(system + "\nReply with ONLY valid JSON. No markdown, no commentary.", user, max_tokens)
    m = re.search(r"\{.*\}|\[.*\]", t, re.S)
    if not m:
        raise ValueError("Model did not return JSON")
    return json.loads(m.group(0))


def _profile_brief(p):
    keys = ["full_name", "location", "current_title", "years_experience", "expected_ctc", "notice_period", "skills", "summary"]
    return {k: p.get(k, "") for k in keys}


def match_job(job, cvs, profile, settings):
    cv_list = [{"id": c["id"], "name": c["name"], "role_tag": c["role_tag"], "cv_text": (c["text"] or "")[:2500]} for c in cvs]
    system = ("You are a strict recruiter-side screener. Score how well a job fits a candidate and choose the best CV. "
              'Return {"score": 0-100, "cv_id": <id>, "reason": "<one sentence>"}. '
              "Penalise location mismatch, seniority mismatch and missing core skills. Be honest; most jobs deserve under 70.")
    user = json.dumps({
        "target_roles": settings["roles"], "preferred_locations": settings["locations"],
        "candidate": _profile_brief(profile), "cvs": cv_list,
        "job": {"title": job["title"], "company": job["company"], "location": job["location"],
                "description": (job["description"] or "")[:3500]}})
    r = ask_json(system, user, 400)
    ids = [c["id"] for c in cvs]
    cv_id = r.get("cv_id") if r.get("cv_id") in ids else ids[0]
    return max(0, min(100, int(r.get("score", 0)))), cv_id, str(r.get("reason", ""))[:300]


def tailor(job, cv, profile):
    system = ("You write short, honest job-application emails. Use ONLY facts found in the CV or profile; never invent "
              'experience. Return {"subject": "...", "body": "..."}. Body: plain text, under 140 words, a greeting, '
              "mention the exact role and company, 2-3 concrete strengths relevant to the job, say the CV is attached, "
              "and sign off with name, phone and LinkedIn when available. No flattery, no clichés.")
    user = json.dumps({
        "profile": {k: v for k, v in profile.items() if v}, "cv_text": (cv["text"] or "")[:3500],
        "job": {"title": job["title"], "company": job["company"], "description": (job["description"] or "")[:3000]}})
    r = ask_json(system, user, 800)
    return r["subject"][:200], r["body"]


def followup(job, profile, original_subject):
    system = 'Write a polite 2-3 sentence follow-up email to a recruiter. Return {"body": "..."}. Plain text, sign with the name given.'
    r = ask_json(system, json.dumps({"name": profile.get("full_name"), "role": job["title"], "company": job["company"],
                                     "original_subject": original_subject}), 300)
    return r["body"]


def map_form(fields, profile, cv_text, job, cover_note):
    system = ("You fill job application forms for a candidate. For each field decide a value using ONLY the profile, "
              "standard answers and CV. Return a JSON object mapping field idx (string) to a value. Rules: "
              "use null when you are not certain or the question needs personal judgment; for select fields return one of "
              "the listed options exactly; for checkbox/radio return true ONLY for the option whose label matches the "
              "right answer, otherwise null; for cover letter / 'why this company' fields use the cover note; "
              "keep answers short and factual.")
    user = json.dumps({
        "profile": {k: v for k, v in profile.items() if v}, "cv_excerpt": (cv_text or "")[:2500],
        "job": {"title": job["title"], "company": job["company"]}, "cover_note": cover_note,
        "fields": [{k: f[k] for k in ("idx", "label", "type", "options", "required")} for f in fields]})
    return ask_json(system, user, 1500)
