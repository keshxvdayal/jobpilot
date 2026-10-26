"""Generic browser form filler (Google Forms, Greenhouse, Lever, Workable, simple career pages)."""
import re
from . import ai

EXTRACT_JS = """() => {
  document.querySelectorAll('[data-jb-idx]').forEach(e => e.removeAttribute('data-jb-idx'));
  const out = []; let i = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const t = (el.type || '').toLowerCase();
    if (['hidden','submit','button','image','reset'].includes(t)) continue;
    const r = el.getBoundingClientRect();
    if (t !== 'file' && (r.width === 0 || r.height === 0)) continue;
    let label = '';
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label) label = el.getAttribute('aria-label') || '';
    if (!label) { const p = el.closest('label'); if (p) label = p.innerText; }
    if (!label) { const li = el.closest('[role=listitem], .field, .application-question, .application-field, fieldset');
                  if (li) label = li.innerText.split('\\n')[0]; }
    if (!label) label = el.placeholder || el.name || '';
    el.setAttribute('data-jb-idx', i);
    const options = el.tagName === 'SELECT' ? [...el.options].map(o => o.text).filter(Boolean) : [];
    out.push({idx: i, type: t || el.tagName.toLowerCase(), label: label.trim().slice(0, 200),
              required: el.required || el.getAttribute('aria-required') === 'true', options});
    i++;
  }
  return out;
}"""
SUBMIT = 'button[type=submit], input[type=submit], button:has-text("Submit"), [role=button]:has-text("Submit")'
NEXT = '[role=button]:has-text("Next"), button:has-text("Next"), button:has-text("Continue")'
CAPTCHA = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha'
SUCCESS = re.compile(r"thank you|thanks for applying|application (has been )?(submitted|received)|"
                     r"your response has been recorded|successfully submitted", re.I)


def _fill(page, f, v):
    loc = page.locator(f'[data-jb-idx="{f["idx"]}"]').first
    try:
        if f["type"] == "select":
            try:
                loc.select_option(label=str(v))
            except Exception:
                match = next((o for o in f["options"] if str(v).lower() in o.lower()), None)
                if match:
                    loc.select_option(label=match)
        elif f["type"] in ("checkbox", "radio"):
            loc.check(force=True)
        else:
            loc.fill(str(v))
    except Exception:
        pass


def apply_form(job, app, cv, profile, settings):
    """Returns (status, note) where status is applied | needs_you | failed."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(settings.get("form_headless", True)))
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if "accounts.google.com" in page.url:
                return "needs_you", "Form requires Google sign-in"
            if page.locator("input[type=password]:visible").count():
                return "needs_you", "Site requires login"
            for _ in range(6):
                fields = page.evaluate(EXTRACT_JS)
                texts = [f for f in fields if f["type"] != "file"]
                mapping = ai.map_form(texts, profile, cv["text"], job, app["body"]) if texts else {}
                missing = []
                for f in texts:
                    v = mapping.get(str(f["idx"]))
                    if v in (None, "", False):
                        if f["required"] and f["type"] not in ("checkbox", "radio"):
                            missing.append(f["label"] or f"field {f['idx']}")
                        continue
                    _fill(page, f, v)
                for f in fields:
                    if f["type"] == "file" and not re.search("cover", f["label"], re.I):
                        page.locator(f'[data-jb-idx="{f["idx"]}"]').first.set_input_files(cv["path"])
                if missing:
                    return "needs_you", ("Needs your answer: " + "; ".join(missing))[:300]
                if settings.get("form_dry_run"):
                    return "needs_you", "Dry run: form filled but not submitted"
                sub = page.locator(SUBMIT).first
                if sub.count():
                    sub.click()
                    page.wait_for_timeout(4000)
                    if SUCCESS.search(page.inner_text("body")):
                        return "applied", "Submitted via form bot"
                    if page.locator(CAPTCHA).count():
                        return "needs_you", "Blocked by CAPTCHA - finish manually"
                    return "needs_you", "Submit not confirmed - please verify"
                nxt = page.locator(NEXT).first
                if nxt.count():
                    nxt.click()
                    page.wait_for_timeout(2000)
                    continue
                return "needs_you", "No submit button found"
            return "needs_you", "Form had too many steps"
        except Exception as e:
            return "failed", f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            browser.close()
