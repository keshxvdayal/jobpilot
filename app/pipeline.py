import datetime
from . import db, ai, collectors, mailer

TODAY = "date('now','localtime')"


def log(level, msg):
    print(f"[{level}] {msg}")
    db.x("INSERT INTO logs(level,msg) VALUES(?,?)", (level, str(msg)[:500]))


def collect():
    s = db.get_settings()
    for name, fn in collectors.REGISTRY.items():
        if not s["sources"].get(name):
            continue
        try:
            items = fn(s)
        except Exception as e:
            log("error", f"{name}: {e}")
            continue
        bl = [b.lower() for b in s["blacklist_companies"]]
        new = 0
        for j in items:
            if any(b and b in j["company"].lower() for b in bl):
                continue
            if db.q("SELECT id FROM jobs WHERE external_id=? OR url=?", (j["external_id"], j["url"]), one=True):
                continue
            db.x("""INSERT INTO jobs(source,external_id,title,company,location,url,description,apply_email,apply_type)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                 (j["source"], j["external_id"], j["title"], j["company"], j["location"], j["url"],
                  j["description"], j["apply_email"], j["apply_type"]))
            new += 1
        log("info", f"{name}: {len(items)} fetched, {new} new")


def match_new():
    s, profile = db.get_settings(), db.get_profile()
    cvs = db.q("SELECT * FROM cvs")
    if not cvs:
        return log("warn", "Upload at least one CV before matching.")
    if not ai.available():
        return log("error", "ANTHROPIC_API_KEY missing - cannot score jobs.")
    jobs = db.q("SELECT * FROM jobs WHERE status='new' ORDER BY id DESC LIMIT ?", (s["max_match_per_run"],))
    kept = 0
    for j in jobs:
        if any(k.lower() in j["title"].lower() for k in s["exclude_keywords"] if k):
            db.x("UPDATE jobs SET status='rejected', reason='Excluded keyword' WHERE id=?", (j["id"],))
            continue
        try:
            score, cv_id, reason = ai.match_job(j, cvs, profile, s)
        except Exception as e:
            log("error", f"match failed: {e}")
            break
        status = "matched" if score >= s["min_score"] else "rejected"
        kept += status == "matched"
        db.x("UPDATE jobs SET score=?, cv_id=?, reason=?, status=? WHERE id=?", (score, cv_id, reason, status, j["id"]))
    log("info", f"Scored {len(jobs)} jobs, {kept} matched")


def prepare():
    s, profile = db.get_settings(), db.get_profile()
    used = db.q(f"SELECT COUNT(*) n FROM applications WHERE date(created_at)={TODAY} AND status!='rejected'", one=True)["n"]
    slots = max(0, s["daily_cap"] - used)
    if not slots:
        return log("info", "Daily cap already filled")
    jobs = db.q("""SELECT * FROM jobs WHERE status='matched'
                   ORDER BY CASE apply_type WHEN 'email' THEN 0 WHEN 'form' THEN 1 ELSE 2 END, score DESC LIMIT ?""", (slots,))
    made = 0
    for j in jobs:
        cv = db.q("SELECT * FROM cvs WHERE id=?", (j["cv_id"],), one=True) or db.q("SELECT * FROM cvs LIMIT 1", one=True)
        try:
            subject, body = ai.tailor(j, cv, profile)
        except Exception as e:
            log("error", f"tailor failed: {e}")
            break
        if j["apply_type"] == "manual":
            status = "needs_you"
        else:
            status = "approved" if s["mode"] == "autopilot" else "pending_approval"
        note = "Apply on the job page - cover note is ready" if status == "needs_you" else None
        db.x("INSERT INTO applications(job_id,cv_id,method,status,subject,body,note) VALUES(?,?,?,?,?,?,?)",
             (j["id"], cv["id"], j["apply_type"], status, subject, body, note))
        db.x("UPDATE jobs SET status='queued' WHERE id=?", (j["id"],))
        made += 1
    log("info", f"Prepared {made} applications ({s['mode']} mode)")


def send_next(force=False):
    s, profile = db.get_settings(), db.get_profile()
    if not force and not (s["send_window_start"] <= datetime.datetime.now().hour < s["send_window_end"]):
        return
    done = db.q(f"SELECT COUNT(*) n FROM applications WHERE status='applied' AND method!='manual' AND date(applied_at)={TODAY}",
                one=True)["n"]
    if done >= s["daily_cap"]:
        return
    a = db.q("SELECT * FROM applications WHERE status='approved' ORDER BY id LIMIT 1", one=True)
    if not a:
        return
    db.x("UPDATE applications SET status='sending' WHERE id=?", (a["id"],))
    job = db.q("SELECT * FROM jobs WHERE id=?", (a["job_id"],), one=True)
    cv = db.q("SELECT * FROM cvs WHERE id=?", (a["cv_id"],), one=True)
    try:
        if a["method"] == "email":
            mailer.send_email(job["apply_email"], a["subject"], a["body"], cv["path"], profile.get("full_name", ""))
            status, note = "applied", f"Emailed {job['apply_email']}"
        else:
            from . import formbot
            status, note = formbot.apply_form(job, a, cv, profile, s)
    except Exception as e:
        status, note = "failed", f"{type(e).__name__}: {str(e)[:200]}"
    stamp = ", applied_at=datetime('now','localtime')" if status == "applied" else ""
    db.x(f"UPDATE applications SET status=?, note=?{stamp} WHERE id=?", (status, note, a["id"]))
    log("info" if status == "applied" else "warn", f"{job['title']} @ {job['company']}: {status} - {note}")


def check_replies():
    rows = db.q("""SELECT a.id, a.subject, a.applied_at, j.apply_email email FROM applications a JOIN jobs j ON j.id=a.job_id
                   WHERE a.status='applied' AND a.method='email' AND a.reply_status IS NULL AND a.applied_at IS NOT NULL""")
    for aid, kind in mailer.find_replies(rows).items():
        db.x("UPDATE applications SET reply_status=? WHERE id=?", (kind, aid))
        log("info", f"Application #{aid}: {kind} received")


def followups():
    s, profile = db.get_settings(), db.get_profile()
    if not s["followups_enabled"] or not mailer.configured():
        return
    rows = db.q(f"""SELECT a.*, j.apply_email, j.title, j.company, j.description, c.path cv_path FROM applications a
                    JOIN jobs j ON j.id=a.job_id LEFT JOIN cvs c ON c.id=a.cv_id
                    WHERE a.status='applied' AND a.method='email' AND a.reply_status IS NULL AND a.followup_sent=0
                    AND a.applied_at <= datetime('now','localtime','-{int(s['followup_days'])} days') LIMIT 20""")
    for a in rows:
        try:
            body = ai.followup(a, profile, a["subject"])
            mailer.send_email(a["apply_email"], "Re: " + a["subject"], body, None, profile.get("full_name", ""))
            db.x("UPDATE applications SET followup_sent=1 WHERE id=?", (a["id"],))
            log("info", f"Follow-up sent: {a['title']} @ {a['company']}")
        except Exception as e:
            log("error", f"follow-up failed: {e}")


def daily_run():
    collect()
    match_new()
    prepare()
