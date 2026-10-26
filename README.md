# JobPilot

Collects jobs from sources that allow it, scores them against your CVs with Claude, writes a tailored email or cover note
for each, and applies by Gmail or by filling the application form in a real browser. You manage everything from a dashboard.

## Run it
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium                              # for the form bot
cp .env.example .env                                     # then fill it in
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000, then: **Profile** -> **CVs** -> **Settings** (roles, locations, cap) -> Overview -> "Run full daily cycle".

## Daily flow
1. 08:00 (configurable): collect jobs, score them, write applications up to your daily cap.
2. **Digest mode**: review and approve on the *Review queue* page. **Autopilot**: they are approved automatically.
3. Approved applications go out one at a time, every N minutes, inside your sending window (50/day fits in ~10 hours at 12 min).
4. Hourly: replies are detected in your inbox (interview / rejection / other). After 6 days with no reply, one follow-up is sent.
5. Anything the bot cannot finish (CAPTCHA, login, unanswerable question) lands in *Needs you*, pre-written.

## Sources
Remotive, RemoteOK, Hacker News "Who is hiring", Adzuna (free API key), Greenhouse and Lever boards (add company slugs in Settings),
plus anything you paste under *Add jobs* (LinkedIn / WhatsApp / Telegram posts, emails inside are extracted).
LinkedIn and Naukri are deliberately not scraped: it breaks their terms and risks your account.

## Notes
- Gmail needs an **App password** (2-step verification on), not your normal password.
- Your profile, CV text and job text are sent to the Claude API to score, write and fill forms.
- Keep it on 127.0.0.1. If you host it, set DASHBOARD_PASSWORD in `.env` and put it behind HTTPS.
- Start in Digest mode, and turn on "Form bot dry run" for your first few form applications to check what it fills.
- Email mostly works only where a job lists an HR address; Remotive/RemoteOK/Adzuna jobs go to *Needs you* with a ready cover note.
