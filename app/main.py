import os, uuid, threading, hashlib, re, base64, secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response
from apscheduler.schedulers.background import BackgroundScheduler
from . import db, pipeline, ai, collectors, mailer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CV_DIR = os.path.join(os.path.dirname(db.DB_PATH) or ".", "cvs")
os.makedirs(CV_DIR, exist_ok=True)
sched = BackgroundScheduler()
_locks = {}


def run_bg(name, fn, *a):
    lock = _locks.setdefault(name, threading.Lock())

    def work():
        if not lock.acquire(blocking=False):
            return
        try:
            fn(*a)
        except Exception as e:
            pipeline.log("error", f"{name} crashed: {e}")
        finally:
            lock.release()
    threading.Thread(target=work, daemon=True).start()


def schedule():
    s = db.get_settings()
    sched.add_job(lambda: run_bg("daily", pipeline.daily_run), "cron", hour=int(s["collect_hour"]), id="daily", replace_existing=True)
    sched.add_job(lambda: run_bg("send", pipeline.send_next), "interval", minutes=max(1, int(s["send_interval_minutes"])),
                  id="send", replace_existing=True)
    sched.add_job(lambda: run_bg("replies", pipeline.check_replies), "interval", minutes=60, id="replies", replace_existing=True)
    sched.add_job(lambda: run_bg("followups", pipeline.followups), "cron", hour=11, id="followups", replace_existing=True)


@asynccontextmanager
async def lifespan(_):
    db.init()
    schedule()
    sched.start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="JobPilot", lifespan=lifespan)


@app.middleware("http")
async def auth(request: Request, call_next):
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if pw:
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                ok = secrets.compare_digest(base64.b64decode(h[6:]).decode().partition(":")[2], pw)
            except Exception:
                pass
        if not ok:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="JobPilot"'})
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


def one(sql, a=()):
    return db.q(sql, a, one=True)["n"]


@app.get("/api/stats")
def stats():
    s = db.get_settings()
    daily = db.q("""SELECT date(applied_at) d, COUNT(*) n FROM applications WHERE status='applied'
                    AND applied_at >= date('now','-13 days','localtime') GROUP BY d""")
    return {
        "cap": s["daily_cap"], "mode": s["mode"],
        "applied_today": one("SELECT COUNT(*) n FROM applications WHERE status='applied' AND method!='manual' AND date(applied_at)=date('now','localtime')"),
        "applied_total": one("SELECT COUNT(*) n FROM applications WHERE status='applied'"),
        "review": one("SELECT COUNT(*) n FROM applications WHERE status='pending_approval'"),
        "approved": one("SELECT COUNT(*) n FROM applications WHERE status IN ('approved','sending')"),
        "needs": one("SELECT COUNT(*) n FROM applications WHERE status='needs_you'"),
        "failed": one("SELECT COUNT(*) n FROM applications WHERE status='failed'"),
        "replies": one("SELECT COUNT(*) n FROM applications WHERE reply_status IS NOT NULL"),
        "interviews": one("SELECT COUNT(*) n FROM applications WHERE reply_status='interview'"),
        "jobs_total": one("SELECT COUNT(*) n FROM jobs"),
        "jobs_matched": one("SELECT COUNT(*) n FROM jobs WHERE status IN ('matched','queued')"),
        "daily": daily,
        "health": {"claude": ai.available(), "gmail": mailer.configured(), "cvs": one("SELECT COUNT(*) n FROM cvs"),
                   "profile": bool(db.get_profile().get("full_name")), "adzuna": bool(os.environ.get("ADZUNA_APP_ID"))},
    }


APP_SQL = """SELECT a.*, j.title, j.company, j.url, j.score, j.reason, j.apply_email, c.name cv_name
             FROM applications a JOIN jobs j ON j.id=a.job_id LEFT JOIN cvs c ON c.id=a.cv_id"""


@app.get("/api/applications")
def applications(status: str = "", limit: int = 300):
    if status:
        return db.q(APP_SQL + " WHERE a.status=? ORDER BY a.id DESC LIMIT ?", (status, limit))
    return db.q(APP_SQL + " ORDER BY a.id DESC LIMIT ?", (limit,))


@app.put("/api/applications/{aid}")
def edit_application(aid: int, body: dict):
    for k in ("subject", "body"):
        if k in body:
            db.x(f"UPDATE applications SET {k}=? WHERE id=?", (body[k], aid))
    return {"ok": True}


@app.post("/api/applications/{aid}/{action}")
def application_action(aid: int, action: str):
    sql = {
        "approve": "UPDATE applications SET status='approved' WHERE id=? AND status IN ('pending_approval','failed')",
        "reject": "UPDATE applications SET status='rejected' WHERE id=?",
        "retry": "UPDATE applications SET status='approved', note=NULL WHERE id=? AND status IN ('failed','needs_you')",
        "done": "UPDATE applications SET status='applied', applied_at=datetime('now','localtime'), note='Marked done by you' WHERE id=?",
    }.get(action)
    if not sql:
        raise HTTPException(404, "unknown action")
    db.x(sql, (aid,))
    return {"ok": True}


@app.post("/api/approve_all")
def approve_all():
    db.x("UPDATE applications SET status='approved' WHERE status='pending_approval'")
    return {"ok": True}


@app.get("/api/jobs")
def jobs(status: str = "", q: str = "", limit: int = 300):
    sql, args = "SELECT id,source,title,company,location,url,apply_type,apply_email,score,reason,status,created_at FROM jobs WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ?)"; args += [f"%{q}%"] * 2
    return db.q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))


def insert_job(source, ext, title, company, location, url, desc, email):
    email = email or collectors.find_email(desc)
    if db.q("SELECT id FROM jobs WHERE external_id=?", (ext,), one=True):
        return False
    db.x("""INSERT INTO jobs(source,external_id,title,company,location,url,description,apply_email,apply_type)
            VALUES(?,?,?,?,?,?,?,?,?)""",
         (source, ext, title, company, location, url, desc, email, collectors.classify(url, email)))
    return True


@app.post("/api/paste")
def paste(body: dict):
    """Paste LinkedIn/WhatsApp/Telegram hiring posts (separate several with a line containing only ---)."""
    added = 0
    for block in [b.strip() for b in re.split(r"\n\s*---+\s*\n", body.get("text", "")) if b.strip()]:
        info = {}
        if ai.available():
            try:
                info = ai.ask_json('Extract from this hiring post: {"title","company","location","email","apply_url"}. Use "" if absent.', block[:4000], 300)
            except Exception:
                pass
        title = info.get("title") or block.split("\n")[0][:120]
        ext = "paste-" + hashlib.sha1(block.encode()).hexdigest()[:16]
        added += insert_job("paste", ext, title, info.get("company", ""), info.get("location", ""),
                            info.get("apply_url") or "", block, info.get("email") or None)
    return {"added": added}


@app.post("/api/jobs/manual")
def manual_job(body: dict):
    ext = "manual-" + uuid.uuid4().hex[:10]
    insert_job("manual", ext, body.get("title", ""), body.get("company", ""), body.get("location", ""),
               body.get("url", ""), body.get("description", ""), body.get("email") or None)
    return {"ok": True}


def extract_text(path):
    try:
        if path.lower().endswith(".pdf"):
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        if path.lower().endswith(".docx"):
            import docx
            return "\n".join(p.text for p in docx.Document(path).paragraphs)
        return open(path, errors="ignore").read()
    except Exception:
        return ""


@app.get("/api/cvs")
def list_cvs():
    return db.q("SELECT id,name,role_tag,path,length(text) chars,created_at FROM cvs ORDER BY id DESC")


@app.post("/api/cvs")
async def upload_cv(file: UploadFile = File(...), name: str = Form(...), role_tag: str = Form("")):
    path = os.path.join(CV_DIR, f"{uuid.uuid4().hex[:8]}_{os.path.basename(file.filename)}")
    with open(path, "wb") as f:
        f.write(await file.read())
    text = extract_text(path)
    if not text.strip():
        raise HTTPException(400, "Could not read any text from this file. Use a text-based PDF or DOCX.")
    db.x("INSERT INTO cvs(name,role_tag,path,text) VALUES(?,?,?,?)", (name, role_tag, path, text))
    return {"ok": True}


@app.delete("/api/cvs/{cid}")
def delete_cv(cid: int):
    r = db.q("SELECT path FROM cvs WHERE id=?", (cid,), one=True)
    if r and os.path.exists(r["path"]):
        os.remove(r["path"])
    db.x("DELETE FROM cvs WHERE id=?", (cid,))
    return {"ok": True}


@app.get("/api/profile")
def get_profile():
    return db.get_profile()


@app.put("/api/profile")
def put_profile(body: dict):
    db.set_kv("profile", {**db.get_profile(), **body})
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: dict):
    db.set_kv("settings", {**db.get_settings(), **body})
    schedule()
    return {"ok": True}


@app.get("/api/logs")
def logs():
    return db.q("SELECT ts,level,msg FROM logs ORDER BY id DESC LIMIT 200")


TASKS = {"collect": pipeline.collect, "match": pipeline.match_new, "prepare": pipeline.prepare,
         "send": lambda: pipeline.send_next(True), "daily": pipeline.daily_run,
         "replies": pipeline.check_replies, "followups": pipeline.followups}


@app.post("/api/run/{task}")
def run_task(task: str):
    if task not in TASKS:
        raise HTTPException(404, "unknown task")
    run_bg("manual-" + task, TASKS[task])
    return {"started": task}
