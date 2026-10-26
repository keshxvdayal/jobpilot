import os, smtplib, imaplib, email, re, mimetypes, datetime
from email.message import EmailMessage


def creds():
    return os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")


def configured():
    u, p = creds()
    return bool(u and p)


def send_email(to, subject, body, cv_path, from_name="", in_reply_to=None):
    user, pw = creds()
    if not (user and pw):
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set in .env")
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{user}>" if from_name else user
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    if cv_path and os.path.exists(cv_path):
        ctype = mimetypes.guess_type(cv_path)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        with open(cv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype=main, subtype=sub, filename=os.path.basename(cv_path).split("_", 1)[-1])
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def classify(text):
    t = text.lower()
    if re.search(r"unfortunately|regret|not moving forward|other candidates|not shortlisted|position (has been )?filled", t):
        return "rejection"
    if re.search(r"interview|schedule|availab|shortlist|call with|discuss further|next round|assessment", t):
        return "interview"
    return "reply"


def find_replies(items):
    """items: [{id, email, subject, applied_at}] -> {id: kind}"""
    out, (user, pw) = {}, creds()
    if not (user and pw) or not items:
        return out
    M = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
    try:
        M.login(user, pw)
        M.select("INBOX", readonly=True)
        for it in items:
            try:
                since = datetime.datetime.strptime(it["applied_at"][:10], "%Y-%m-%d").strftime("%d-%b-%Y")
                subj = re.sub(r'["\\]', "", it["subject"])
                _, data = M.search(None, "OR", "FROM", f'"{it["email"]}"', "SUBJECT", f'"{subj}"', "SINCE", since)
                ids = data[0].split()
                if not ids:
                    continue
                _, msgdata = M.fetch(ids[-1], "(BODY.PEEK[])")
                m = email.message_from_bytes(msgdata[0][1])
                text = ""
                for part in (m.walk() if m.is_multipart() else [m]):
                    if part.get_content_type() == "text/plain":
                        text = part.get_payload(decode=True).decode(errors="ignore")[:1500]
                        break
                out[it["id"]] = classify((m.get("Subject") or "") + " " + text)
            except Exception:
                continue
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out
