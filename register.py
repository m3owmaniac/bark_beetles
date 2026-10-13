#!/usr/bin/env python3
"""Register for Mt. SAC classes (Banner 9 Self-Service) the moment registration opens.

Does what you'd do by hand, one class at a time in priority order:
  Register for Classes -> pick term -> Continue -> "Enter CRNs" tab ->
  type CRN -> Add to Summary -> Action: **Web Registered** -> Submit

If a CRN definitely can't be registered (full: only Waitlist/Remove offered,
or Banner shows an error on it after Submit), it's set to Remove + Submit and
the backup CRN for that class is tried.

If the site is just slow or broken (timeouts, error pages, no response), that
is NOT treated as "full": it reloads and tries the same CRN again, until
GIVE_UP_AFTER_MINUTES. It only ever reports REGISTERED when Banner's summary
shows the class as registered, and it re-checks that on a fresh page load
at the end.
"""

import argparse
import ctypes
import datetime as dt
import email.utils
import getpass
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

# ============ EDIT THESE ============
# One entry per class, highest priority first. Within an entry, CRNs after
# the first are backups, tried only if the one before it definitely fails.
CLASSES = [
    ["CRN1ST", "CRN2NDSAMECLASSASFIRST"],   # 1st: CRN, or ANOTHERCRN if FIRSTCRN doesn't work or is full
    ["CRNEXTRACLASSIFNEEDED"],            # 2nd
]
TERM = "Winter 2027"                 # as shown in the term dropdown
OPEN_AT = "2026-09-24 08:00:00"      # Pacific time

# Phone notifications: in the ntfy app, tap + and subscribe to this topic
# (server ntfy.sh). Set to "" to turn notifications off.
NTFY_URL = "https://ntfy.sh/USEYOUROWNHEREIFYOUWANTNOTIFICATIONS"
# ====================================

BASE = "https://prodrg.mtsac.edu/StudentRegistrationSsb/ssb"
TZ = ZoneInfo("America/Los_Angeles")
HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser-profile"
SHOTS_DIR = HERE / "screenshots"

KEEPALIVE_SECONDS = 120      # reload every 2 min so Banner doesn't time out
PREP_SECONDS = 45            # open the term page this long before OPEN_AT
GIVE_UP_AFTER_MINUTES = 30   # keep retrying through errors/slowness this long
ADD_WAIT_SECONDS = 20        # how long to wait for Add to Summary under load
SUBMIT_WAIT_SECONDS = 45     # how long to wait for Submit under load

# "Not open yet" -> retry, never counts as a real failure.
NOT_OPEN = re.compile(
    r"time ticket|not permitted|not open|not yet|outside.*registration|"
    r"registration.*(closed|not available|unavailable)|no registration", re.I)
# Server trouble -> retry, never counts as a real failure.
OVERLOAD = re.compile(
    r"service unavailable|internal server error|bad gateway|gateway time-?out|"
    r"too many requests|try again later|temporarily|timed? ?out|unexpected error|"
    r"system error|server error|\b50[0234]\b|\b429\b", re.I)
ERROR_STATUS = re.compile(r"error|prevent|fail|denied|not allowed", re.I)


class Retry(Exception):
    """Something transient (slow site, error page, lost page): try again."""


# ---------------------------------------------------------------- helpers

def now():
    return dt.datetime.now(TZ)


def log(msg):
    print(f"[{now():%H:%M:%S.%f}"[:-3] + f"] {msg}", flush=True)


PICTURES = True   # turned off while registering: pictures cost seconds


def shot(page, name):
    """Save the page's HTML (instant; helps debug a stuck step) and, when
    time isn't critical, a screenshot."""
    SHOTS_DIR.mkdir(exist_ok=True)
    stem = SHOTS_DIR / f"{now():%m%d-%H%M%S}-{name}"
    try:
        Path(f"{stem}.html").write_text(page.content())
        if PICTURES:
            page.screenshot(path=f"{stem}.png", timeout=5000, animations="disabled")
            log(f"screenshot: {stem}.png")
    except Exception as e:  # never let a screenshot kill the run
        log(f"(screenshot failed: {e})")


# ---------------------------------------------------------------- phone alerts

_notify_threads = []
_last_problem = {}


def notify(title, message, priority="default", tags=""):
    """Send a phone notification via ntfy, in the background so it never
    slows down registering."""
    if not NTFY_URL:
        return

    def send():
        try:
            req = urllib.request.Request(
                NTFY_URL, data=message.encode(), method="POST",
                headers={"Title": title.encode("ascii", "ignore").decode(),
                         "Priority": priority, "Tags": tags})
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:
            log(f"(phone notification failed: {e})")

    t = threading.Thread(target=send, daemon=True)
    t.start()
    _notify_threads.append(t)


def notify_problem(key, title, message, every_seconds=180):
    """Error alerts, at most one per `every_seconds` per kind of problem."""
    if time.monotonic() - _last_problem.get(key, -1e9) >= every_seconds:
        _last_problem[key] = time.monotonic()
        notify(title, message, "high", "warning")


def flush_notifications():
    for t in _notify_threads:
        t.join(timeout=15)


# Mt. SAC login, typed in at start-up and kept only in memory.
CREDS = None


def login_again(page, base):
    """Logged out: fill in the Mt. SAC login and wait for Duo (up to 4 min)."""
    if not CREDS:
        notify_problem("login", "LOGGED OUT",
                       "The script got logged out and has no login to use. Log in on the computer.")
        return False
    user, password = CREDS
    log("Logged out: logging back in automatically...")
    notify("Logged out: logging back in",
           "If Duo sends you a push, APPROVE it so registration can continue.", "urgent", "key")
    deadline = time.monotonic() + 240
    submitted = 0
    while time.monotonic() < deadline:
        if where(page, base) != "login":
            log("logged back in")
            notify("Logged back in", "Continuing.", "default", "white_check_mark")
            return True
        try:
            pw_box = page.locator("input[type=password]:visible").first
            user_box = page.locator("input[type=email]:visible, input[type=text]:visible, "
                                    "input[name*=user i]:visible").first
            if user_box.count() and not user_box.input_value():
                user_box.fill(user)
                if not pw_box.count():          # two-step login page
                    user_box.press("Enter")
            if pw_box.count() and not pw_box.input_value() and submitted < 3:
                pw_box.fill(password)
                pw_box.press("Enter")
                submitted += 1
            for label in ["Yes, this is my device", "Trust browser", "Stay signed in", "Yes"]:
                b = page.locator(f"button:has-text('{label}'):visible, "
                                 f"input[value='{label}']:visible").first
                if b.count():
                    b.click()
                    break
        except PWError:
            pass
        time.sleep(1.5)
    notify_problem("login", "STILL LOGGED OUT",
                   "Couldn't log back in (Duo not approved?). Will keep trying.")
    return False


def backoff(attempt):
    """Wait a bit longer after each failure (1s .. 5s), so we don't hammer
    a struggling server but still retry quickly."""
    time.sleep(min(5, attempt) * random.uniform(0.8, 1.2))


def keep_awake():
    """Stop the computer from going to sleep while the script runs."""
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x1 | 0x2)
    elif sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])
    else:  # Linux: hold a systemd sleep/idle lock until this script exits
        try:
            subprocess.Popen(
                ["systemd-inhibit", "--what=sleep:idle", "--who=register.py",
                 "--why=Waiting for class registration", "--mode=block",
                 "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log("(couldn't block sleep; turn off sleep in Power settings)")


def first_visible(page, selectors, timeout=5):
    """Return the first locator (from a list of fallbacks) that is visible."""
    deadline = time.monotonic() + timeout
    while True:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.is_visible():
                    return loc
            except PWError:
                pass
        if time.monotonic() > deadline:
            raise Retry(f"couldn't find on page: {selectors[-1]}")
        time.sleep(0.1)


def messages(page):
    """Text of Banner's visible notification pop-ups (errors, confirmations)."""
    try:
        texts = page.eval_on_selector_all(
            "[class*='notification'], [role='alert'], .error-message",
            # table rows can carry 'notification' classes too: those aren't pop-ups
            "els => els.filter(e => e.offsetParent !== null && !e.closest('table, [role=grid]'))"
            ".map(e => e.innerText)")
    except PWError:
        return []
    boiler = re.compile(r"Use TAB to browse through notifications.*?notifications|"
                        r"Flash notification|^\s*\d*\s*Notifications", re.I | re.S)
    seen, out = set(), []
    for t in texts:
        t = re.sub(r"\s+", " ", boiler.sub(" ", t)).strip()
        # skip containers whose text is already covered by a child's
        if t and t not in seen and not any(t in s for s in seen):
            seen.add(t)
            out.append(t)
    return out


def page_text(page):
    try:
        return page.inner_text("body", timeout=5000)
    except PWError:
        return ""


def where(page, base):
    url = page.url
    if urlparse(url).hostname != urlparse(base).hostname:
        return "login"
    if "termSelection" in url:
        return "term"
    if "classRegistration" in url:
        return "classreg"
    return "other"


def goto(page, url):
    """Navigate; raise Retry on timeouts and server error pages."""
    try:
        resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
    except PWError as e:
        raise Retry(f"page didn't load ({str(e).splitlines()[0][:80]})")
    if resp is not None and resp.status >= 500:
        raise Retry(f"server error page (HTTP {resp.status})")
    if resp is not None and resp.status == 429:
        raise Retry("server says too many requests (HTTP 429)")
    return resp


def server_clock_offset(resp):
    """Seconds the server's clock is ahead of ours (from the HTTP Date header)."""
    try:
        server = email.utils.parsedate_to_datetime(resp.headers["date"])
        return (server - dt.datetime.now(dt.timezone.utc)).total_seconds()
    except Exception:
        return None


# ---------------------------------------------------------------- summary rows

# Finds the summary row for a CRN: a visible table row that mentions the CRN
# (ignoring dropdown text) and has no rows nested inside it. Reads the Status
# column by its header when it can.
ROW_JS = r"""
crn => {
  const re = new RegExp('(^|\\D)' + crn + '(\\D|$)');
  // The Action cell is a dropdown (a hidden <select> with a select2 widget on
  // top, whose screen-reader text reads "Action combobox ... **Web Registered**").
  // Its text must never be mistaken for the Status.
  const ACTION = 'select, option, .select2-container, [role=combobox], .select2-offscreen, .ui-helper-hidden-accessible';
  const isAction = c => !!c.querySelector(ACTION) || /combobox/i.test(c.textContent);
  const cellText = c => {
    const clone = c.cloneNode(true);
    clone.querySelectorAll(ACTION).forEach(e => e.remove());
    return clone.textContent.replace(/\s+/g, ' ').trim();
  };
  const STATUS_WORDS = /^(pending|registered|\**\s*web registered|errors?\b|waitlist|wait-?listed|deleted|dropped|\**\s*web dropped|enrolled)/i;
  let found = null;
  for (const row of document.querySelectorAll('tr, [role=row]')) {
    if (row.offsetParent === null || row.querySelector('tr, [role=row]')) continue;
    const cells = [...row.querySelectorAll('td, [role=gridcell], [role=cell]')];
    if (!cells.length) continue;
    const texts = cells.map(c => isAction(c) ? '' : cellText(c));
    if (!texts.some(t => re.test(t))) continue;
    // Status cell: 1) marked as status, 2) under a "Status" header,
    // 3) the one plain cell whose text looks like a status.
    let status = null;
    cells.forEach((c, i) => {
      if (status === null && !isAction(c) &&
          /status/i.test((c.getAttribute('data-property') || '') + ' ' + c.className))
        status = texts[i];
    });
    if (status === null) {
      for (let a = row.parentElement; a && status === null; a = a.parentElement) {
        const heads = [...a.querySelectorAll('th, [role=columnheader]')]
          .filter(h => h.offsetParent !== null).map(h => h.textContent.trim());
        if (!heads.length) continue;
        const i = heads.findIndex(h => /^status$/i.test(h));
        if (i >= 0 && i < texts.length && texts[i]) status = texts[i];
        break;
      }
    }
    if (status === null) {
      const i = texts.findIndex(t => STATUS_WORDS.test(t));
      if (i >= 0) status = texts[i];
    }
    const sel = row.querySelector('select');
    found = {row, sel, text: texts.filter(t => t).join(' | '), status: status || ''};
  }
  return found;
}
"""

ROW_INFO_JS = "crn => { const f = (" + ROW_JS + """)(crn);
  if (!f) return null;
  return {text: f.text, status: f.status,
          options: f.sel ? [...f.sel.options].map(o => o.text.trim()) : []};
}"""

SET_ACTION_JS = "([crn, pattern]) => { const f = (" + ROW_JS + """)(crn);
  const sel = f && f.sel;
  if (!sel) return null;
  const opt = [...sel.options].find(o => new RegExp(pattern, 'i').test(o.text.trim()));
  if (!opt) return null;
  if (sel.value !== opt.value) {
    sel.value = opt.value;
    sel.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return opt.text.trim();
}"""

WEB_REGISTERED = r"web\s*registered"
REMOVE = r"^remove$|^delete$"
NONE = r"^none$"
DROP = r"drop"


def row_info(page, crn):
    try:
        return page.evaluate(ROW_INFO_JS, crn)
    except PWError:
        return None


def set_action(page, crn, pattern):
    try:
        return page.evaluate(SET_ACTION_JS, [crn, pattern])
    except PWError:
        return None


def is_registered(info):
    """True only if the row's Status cell says registered (and nothing about
    errors). The Action dropdown's text is never part of the status."""
    if not info:
        return False
    st = info["status"] or ""
    return (bool(re.search(r"registered|enrolled", st, re.I))
            and not ERROR_STATUS.search(st)
            and not re.search(r"pending|waitlist|drop|delet", st, re.I))


def has_error(info):
    return bool(info and ERROR_STATUS.search(info["status"] or ""))


# ---------------------------------------------------------------- navigation

def select_term(page, base, term):
    goto(page, f"{base}/term/termSelection?mode=registration")
    try:
        box = first_visible(page, ["#s2id_txt_term", ".select2-container"], 20)
    except Retry:
        log("(term dropdown didn't show; reloading the term page)")
        goto(page, f"{base}/term/termSelection?mode=registration")
        box = first_visible(page, ["#s2id_txt_term", ".select2-container"], 20)
    box.click()
    labels = page.locator(".select2-result-label")
    try:
        labels.first.wait_for(state="visible", timeout=20000)
    except PWError:
        raise Retry("term list didn't load")
    page.keyboard.type(term, delay=20)  # filters the list
    exact = labels.filter(has_text=re.compile(rf"^\s*{re.escape(term)}\s*$"))
    # Fall back to e.g. "Winter 2027 (Jan 4 - Feb 12)", but never "(View Only)".
    loose = labels.filter(has_text=term).filter(has_not_text=re.compile("view only", re.I))
    deadline = time.monotonic() + 5
    while not (exact.count() or loose.count()) and time.monotonic() < deadline:
        time.sleep(0.1)
    if exact.count():
        exact.first.click()
    elif loose.count():
        loose.first.click()
    else:
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")  # unfilter to show what IS listed
        time.sleep(1)
        found = [t.strip() for t in labels.all_inner_texts()]
        page.keyboard.press("Escape")
        raise Retry(f"'{term}' isn't in the term list (list shows: {found})")
    chosen = page.locator("#s2id_txt_term, .select2-container").first.inner_text()
    if term not in chosen:
        raise Retry(f"term dropdown shows {chosen!r}, not {term!r}")


def check_term_listed(page, base, term):
    """Before the open time: make sure the term's in the dropdown (no Continue)."""
    for attempt in range(1, 4):
        try:
            select_term(page, base, term)
            log(f"CHECK OK: '{term}' is in the term dropdown")
            return
        except Exception as e:
            err = str(e).splitlines()[0]
            log(f"(check try {attempt}: {err})")
        time.sleep(3)
    log(f"!!! CHECK: couldn't confirm '{term}' in the term dropdown: {err}")
    notify(f"Check: {term} not confirmed", err[:300], "high", "warning")
    log("    (It may only appear at the open time; the script will keep trying then.)")


def click_continue(page, seconds):
    """Click Continue on a freshly loaded term page. Returns True once the
    browser starts loading Register for Classes; raises Retry as soon as
    Banner shows a message instead (e.g. "not permitted yet")."""
    first_visible(page, ["#term-go", "button:has-text('Continue')"]).click(timeout=5000)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if "classRegistration" in page.url:
            return True
        msgs = messages(page)
        if msgs:
            raise Retry(f"Continue didn't go through: {' | '.join(msgs)[:200]}")
        time.sleep(0.1)
    raise Retry("Continue didn't go through (no answer)")


TAB = ["#enterCRNs-tab", "a[href='#enterCRNs']", "[role='tab']:has-text('Enter CRNs')",
       "a:has-text('Enter CRNs')"]


def get_to_class_page(page, base, term, give_up_at):
    """Get onto the Register for Classes page for the term, however long it takes.

    "On the page" means the Enter CRNs tab is visible and usable. We never
    wait for the browser's full 'load' event: Banner pages are usable long
    before every last file finishes loading."""
    attempt = 0
    while True:
        attempt += 1
        try:
            if page.is_closed():
                raise RuntimeError("browser page was closed")
            spot = where(page, base)
            if spot == "login":
                log("!!! On the login page (logged out?). Trying to get back in...")
                shot(page, "logged-out")
                if not login_again(page, base):
                    raise Retry("still logged out")
                continue
            if spot == "classreg":
                try:
                    first_visible(page, TAB, 20)   # still loading? give it a moment
                    if attempt > 1:
                        log(f"on the Register for Classes page (attempt {attempt})")
                    return
                except Retry:
                    log("Register for Classes page isn't usable; reloading it")
                    goto(page, f"{base}/classRegistration/classRegistration")
                    first_visible(page, TAB, 30)
                    return
            # On the term page (or anywhere else): pick the term, Continue.
            select_term(page, base, term)
            click_continue(page, 30)
            continue  # loop back: the classreg branch waits for the tab
        except Retry as e:
            log(f"retry {attempt}: {e}")
            if NOT_OPEN.search(str(e)) and now() < give_up_at:
                # e.g. "You have no Registration Time Ticket for the current
                # time": not open yet. Each retry reloads the term page and
                # re-selects the term, which is itself ~1-2s, so retry fast.
                time.sleep(0.5)
                continue
        except Exception as e:  # anything unexpected: log it, keep going
            log(f"retry {attempt}: unexpected problem: {str(e).splitlines()[0][:200]}")
        if now() > give_up_at:
            shot(page, "gave-up-getting-in")
            raise RuntimeError("couldn't get to the registration page; gave up")
        backoff(attempt)


def reload_class_page(page, base):
    try:
        goto(page, f"{base}/classRegistration/classRegistration")
        first_visible(page, TAB, 30)   # usable, even if still loading extras
    except (Retry, PWError):
        pass


# ---------------------------------------------------------------- one CRN

def add_crn(page, crn):
    """Type one CRN on the Enter CRNs tab and click Add to Summary."""
    first_visible(page, TAB, 20).click()
    boxes = page.locator("input[id^='txt_crn']")
    try:
        boxes.first.wait_for(state="visible", timeout=10000)
    except PWError:
        raise Retry("CRN box didn't appear")
    for i in range(1, boxes.count()):  # clear leftovers from an earlier CRN
        if boxes.nth(i).is_visible():
            boxes.nth(i).fill("")
    box = boxes.first
    box.fill("")
    # Type it like a person (real key presses), then Tab out so Banner
    # looks the CRN up, and give that lookup a moment.
    box.press_sequentially(crn, delay=30)
    box.press("Tab")
    try:
        page.wait_for_load_state("networkidle", timeout=800)
    except PWError:
        pass
    first_visible(page, ["#addCRNbutton", "button:has-text('Add to Summary')"]).click()
    log(f"{crn}: typed it, clicked Add to Summary")


def wait_for(page, crn, check, seconds):
    """Poll until check(row_info) is true or a new message mentions the CRN."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        info = row_info(page, crn)
        if check(info):
            return info
        time.sleep(0.2)
    return row_info(page, crn)


def submit(page):
    first_visible(page, ["#saveButton", "button:has-text('Submit')"]).click()


def clear_row(page, crn, dry_run):
    """Take a failed CRN out of the summary: Remove + Submit (or at least set
    it to None so it can't be submitted by accident later)."""
    info = row_info(page, crn)
    if not info or not info["options"]:
        return
    if set_action(page, crn, REMOVE):
        if dry_run:
            log(f"{crn}: DRY RUN: set to Remove (would Submit)")
            return
        log(f"{crn}: Remove + Submit")
        try:
            submit(page)
            wait_for(page, crn, lambda i: i is None, 20)
        except Exception as e:
            log(f"{crn}: (remove didn't finish: {e}); moving on")
    else:
        set_action(page, crn, NONE)
        log(f"{crn}: no Remove option (options: {info['options']}); set Action to None")


def notify_registered(crn, info):
    notify(f"REGISTERED {crn}", f"Status column: {info['status']}", "high", "white_check_mark")


def try_crn(page, base, term, crn, dry_run, give_up_at):
    """Returns "registered", "dry-ok", "failed" (definitely can't get this CRN)
    or "gave-up" (site kept misbehaving until give_up_at)."""
    attempt = 0
    while True:
        attempt += 1
        if now() > give_up_at:
            shot(page, f"{crn}-gave-up")
            notify(f"GAVE UP on {crn}", "The site kept failing for "
                   f"{GIVE_UP_AFTER_MINUTES} min. Try by hand!", "urgent", "rotating_light")
            return "gave-up"
        try:
            get_to_class_page(page, base, term, give_up_at)
            info = row_info(page, crn)

            if is_registered(info):
                log(f"{crn}: summary shows it's registered: {info['status']}")
                notify_registered(crn, info)
                return "registered"

            if not info or not info["options"]:
                add_crn(page, crn)
                info = wait_for(page, crn, lambda i: bool(i and i["options"]), ADD_WAIT_SECONDS)
                msgs = messages(page)
                for m in msgs:
                    log(f"message: {m}")
                if not info or not info["options"]:
                    about_crn = [m for m in msgs if crn in m]
                    if about_crn and not any(OVERLOAD.search(m) or NOT_OPEN.search(m)
                                             for m in about_crn):
                        log(f"{crn}: Banner rejected it: {' | '.join(about_crn)}")
                        notify(f"{crn} rejected", " | ".join(about_crn)[:300], "high", "x")
                        shot(page, f"{crn}-rejected")
                        return "failed"
                    raise Retry("CRN didn't show up in the summary (site slow?)")

            log(f"{crn}: in summary: {info['text']}  |  Action options: {info['options']}")
            if not set_action(page, crn, WEB_REGISTERED):
                log(f"{crn}: no Web Registered option (full/closed)")
                notify(f"{crn} can't be registered",
                       f"Only offered: {', '.join(info['options'])} (full?). Removing it.",
                       "high", "x")
                shot(page, f"{crn}-no-web-registered")
                clear_row(page, crn, dry_run)
                return "failed"
            log(f"{crn}: Action set to Web Registered")

            if dry_run:
                shot(page, f"{crn}-dry-run")
                log(f"{crn}: DRY RUN: would click Submit now. Nothing registered.")
                return "dry-ok"

            submit(page)
            log(f"{crn}: clicked Submit, waiting for Banner...")
            info = wait_for(page, crn, lambda i: is_registered(i) or has_error(i),
                            SUBMIT_WAIT_SECONDS)
            msgs = messages(page)
            for m in msgs:
                log(f"message: {m}")
            shot(page, f"{crn}-after-submit")

            if is_registered(info):
                log(f"{crn}: REGISTERED ({info['status']})")
                notify_registered(crn, info)
                return "registered"
            if has_error(info):
                text = info["status"] + " " + " ".join(msgs)
                if NOT_OPEN.search(text) or OVERLOAD.search(text):
                    raise Retry(f"Banner error that's worth retrying: {info['status']}")
                log(f"{crn}: FAILED: {info['text']}")
                notify(f"{crn} failed", f"Banner: {info['status']} {' '.join(msgs)}"[:300],
                       "high", "x")
                clear_row(page, crn, dry_run)
                return "failed"
            # No answer at all. Maybe it went through and the page just didn't
            # update, so reload and look before doing anything else.
            log(f"{crn}: no answer from Submit; reloading to see what happened")
            reload_class_page(page, base)
            info = row_info(page, crn)
            if is_registered(info):
                log(f"{crn}: REGISTERED (confirmed after reload)")
                notify_registered(crn, info)
                return "registered"
            raise Retry("Submit didn't go through")

        except Retry as e:
            log(f"{crn}: retry {attempt}: {e}")
            notify_problem("retry", "Site trouble, still retrying",
                           f"{crn}, try {attempt}: {e}"[:300])
        except Exception as e:
            log(f"{crn}: retry {attempt}: unexpected problem: {str(e).splitlines()[0][:200]}")
            notify_problem("retry", "Site trouble, still retrying",
                           f"{crn}, try {attempt}: {str(e).splitlines()[0]}"[:300])
        if attempt <= 3:
            shot(page, f"{crn}-retry-{attempt}")
        backoff(attempt)
        reload_class_page(page, base)


def drop_crn(page, base, term, crn, give_up_at):
    """Test mode only: drop a class this run registered."""
    for attempt in range(1, 6):
        try:
            get_to_class_page(page, base, term, give_up_at)
            info = row_info(page, crn)
            if not info or not is_registered(info):
                log(f"{crn}: not showing as registered any more ({info and info['status']})")
                return True
            picked = set_action(page, crn, DROP)
            if not picked:
                log(f"{crn}: no Drop option (options: {info['options']})")
                return False
            log(f"{crn}: Action set to {picked}, Submit")
            submit(page)
            info = wait_for(page, crn, lambda i: not is_registered(i), SUBMIT_WAIT_SECONDS)
            for m in messages(page):
                log(f"message: {m}")
            reload_class_page(page, base)
            if not is_registered(row_info(page, crn)):
                log(f"{crn}: DROPPED")
                return True
        except Exception as e:
            log(f"{crn}: drop retry {attempt}: {str(e).splitlines()[0][:200]}")
        backoff(attempt)
    return False


# ---------------------------------------------------------------- run

def register(page, base, term, classes, dry_run, give_up_at):
    results = []
    for choices in classes:
        got, outcome = None, None
        for crn in choices:
            outcome = try_crn(page, base, term, crn, dry_run, give_up_at)
            if outcome in ("registered", "dry-ok"):
                got = crn
                break
            if outcome == "gave-up":  # site never worked: backups won't either
                break
            if crn != choices[-1]:
                nxt = choices[choices.index(crn) + 1]
                log(f"{crn} definitely didn't work, trying backup {nxt}...")
                notify(f"Trying backup {nxt}", f"{crn} didn't work, so trying {nxt}.", "high")
        results.append((choices, got, outcome))
    return results


def final_report(page, base, term, results, dry_run, give_up_at):
    """Re-check every CRN on a fresh page load, so the report is Banner's word."""
    confirmed = {}
    wanted = [got for _, got, _ in results if got]
    for attempt in range(1, 7) if not dry_run else ():
        try:
            reload_class_page(page, base)
            get_to_class_page(page, base, term, now() + dt.timedelta(minutes=2))
            for choices, _, _ in results:
                for crn in choices:
                    confirmed[crn] = row_info(page, crn)
            if all(is_registered(confirmed.get(c)) for c in wanted):
                break
            log(f"(final re-check {attempt}: page didn't show everything yet, reloading)")
        except Exception as e:
            log(f"(final re-check {attempt} failed: {str(e).splitlines()[0][:120]})")
        backoff(attempt)
    if not dry_run:
        shot(page, "final")
    lines = []
    log("=" * 60)
    log(f"RESULTS ({term})")
    for choices, got, outcome in results:
        label = " / ".join(choices)
        if dry_run:
            line = f"dry run OK with {got}" if got else f"dry run: none worked ({outcome})"
        elif got and is_registered(confirmed.get(got)):
            line = f"REGISTERED {got}  (confirmed on a fresh page load)"
        elif got:
            line = (f"probably REGISTERED {got}, but the final re-check couldn't "
                    f"confirm it. CHECK BY HAND.")
        elif outcome == "gave-up":
            line = "NOT registered: the site kept failing until the script gave up"
        else:
            line = "NOT registered (all choices were full or rejected)"
        # Also catch a CRN that went through even though we thought it failed.
        for crn in choices:
            if crn != got and is_registered(confirmed.get(crn)):
                line += f"  [NOTE: {crn} also shows as registered!]"
        log(f"  {label}: {line}")
        lines.append(f"{label}: {line}")
    log("=" * 60)
    notify(f"Final results ({term})", "\n".join(lines),
           "high" if all(g for _, g, _ in results) else "urgent", "clipboard")
    return confirmed


def parse_time(s):
    if s == "now":
        return now()
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crns", nargs="+", metavar="CRN[/BACKUP]",
                    help="e.g. --crns 30169/30168 30525 (overrides CLASSES)")
    ap.add_argument("--term", default=TERM)
    ap.add_argument("--at", default=OPEN_AT, help="'YYYY-MM-DD HH:MM:SS' Pacific, or 'now'")
    ap.add_argument("--dry-run", action="store_true", help="do everything except click Submit")
    ap.add_argument("--drop-after", action="store_true",
                    help="TEST ONLY: drop the classes again at the end (needs --term)")
    ap.add_argument("--ntfy", default=None, help="ntfy topic URL (overrides NTFY_URL)")
    ap.add_argument("--base", default=BASE, help=argparse.SUPPRESS)
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)  # skip login prompt (testing)
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.drop_after and ("--term" not in " ".join(sys.argv) or args.term == TERM):
        sys.exit(f"--drop-after is only for tests on another term (it would drop your {TERM} classes).")

    global NTFY_URL, CREDS, PICTURES
    if args.ntfy is not None:
        NTFY_URL = args.ntfy
    classes = [c.split("/") for c in args.crns] if args.crns else CLASSES
    open_at = parse_time(args.at)
    give_up_at = open_at + dt.timedelta(minutes=GIVE_UP_AFTER_MINUTES)
    log(f"term={args.term}  classes={' then '.join('/'.join(c) for c in classes)}  "
        f"opens={open_at:%a %b %d %I:%M:%S %p} PT"
        + ("  [DRY RUN]" if args.dry_run else "") + ("  [DROP AFTER]" if args.drop_after else ""))
    keep_awake()

    # Login for automatic re-login if the session ever drops. Only kept in
    # memory while the script runs; never written anywhere.
    if os.environ.get("MTSAC_USER"):
        CREDS = (os.environ["MTSAC_USER"], os.environ.get("MTSAC_PASS", ""))
    elif not args.yes:
        print("\nFor automatic re-login if you get logged out (press Enter to skip):")
        user = input("  Mt. SAC username: ").strip()
        if user:
            CREDS = (user, getpass.getpass("  Mt. SAC password (hidden as you type): "))
            print("  Saved in memory only, for this run.")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=args.headless,
            executable_path=os.environ.get("CHROMIUM_PATH") or None,
            viewport={"width": 1400, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(15000)
        try:
            resp = page.goto(f"{args.base}/registration", wait_until="domcontentloaded", timeout=60000)
            if not args.yes:
                print("\n" + "=" * 64)
                print(" Log in in the browser window (including Duo).")
                print(" When you can see the 'Register for Classes' page,")
                print(" come back here and press Enter.")
                print("=" * 64)
                while True:
                    input()
                    if where(page, args.base) != "login":
                        break
                    print("Still on the login page. Finish logging in, then press Enter again.")
                resp = page.goto(f"{args.base}/registration", wait_until="domcontentloaded",
                                 timeout=60000)
            log("Logged in. Don't close the browser window.")
            notify("Registration script is running",
                   f"{args.term}: {' then '.join('/'.join(c) for c in classes)} at "
                   f"{open_at:%I:%M:%S %p} PT. You'll get a notification for each class.",
                   "default", "hourglass")

            # Fire early if the school's clock is ahead of ours (clicking
            # early is harmless: it just says "not open" and retries).
            offset = server_clock_offset(resp) if resp else None
            if offset is not None:
                log(f"school server clock vs this computer: {offset:+.1f}s")
                if offset > 1:
                    open_at -= dt.timedelta(seconds=offset)
                    log(f"will fire {offset:.0f}s early to match the school's clock")

            if (open_at - now()).total_seconds() > PREP_SECONDS + 10:
                check_term_listed(page, args.base, args.term)
                goto(page, f"{args.base}/registration")

            # Keep the session alive until just before the open time.
            prep_at = open_at - dt.timedelta(seconds=PREP_SECONDS)
            while now() < prep_at:
                time.sleep(max(0, min(KEEPALIVE_SECONDS, (prep_at - now()).total_seconds())))
                if now() >= prep_at:
                    break
                try:
                    goto(page, f"{args.base}/registration")
                except Retry as e:
                    log(f"(keep-alive reload failed: {e}; will retry)")
                if where(page, args.base) == "login":
                    log("!!! Logged out during the wait. Trying to recover...")
                    shot(page, "logged-out")
                    if login_again(page, args.base):
                        goto(page, f"{args.base}/registration")
                else:
                    log(f"session alive; {(open_at - now()).total_seconds() / 60:.0f} min to go")

            try:
                select_term(page, args.base, args.term)
                log(f"term selected: {args.term}; waiting for {open_at:%I:%M:%S %p}")
            except Exception as e:
                log(f"(couldn't pre-select the term yet: {e}; will keep trying at open time)")
            while now() < open_at:
                time.sleep(min(0.05, max(0, (open_at - now()).total_seconds())))
            log("GO")
            if where(page, args.base) == "term" and page.locator("#term-go").count():
                try:  # fast path: the term's already picked, just Continue
                    click_continue(page, 30)
                except Exception as e:
                    log(f"not open yet: {e}")  # get_to_class_page() retries

            PICTURES = False
            results = register(page, args.base, args.term, classes, args.dry_run, give_up_at)
            PICTURES = True
            final_report(page, args.base, args.term, results, args.dry_run, give_up_at)

            if args.drop_after and not args.dry_run:
                log("TEST: dropping the classes this run registered...")
                for _, got, _ in results:
                    if not got:
                        continue
                    if drop_crn(page, args.base, args.term, got, now() + dt.timedelta(minutes=5)):
                        notify(f"Test: dropped {got}", "It no longer shows as registered.",
                               "default", "wastebasket")
                    else:
                        log(f"!!! {got} may STILL BE REGISTERED. Drop it by hand!")
                        notify(f"{got} may still be registered",
                               "Test drop didn't confirm. Drop it by hand!", "urgent", "warning")
                shot(page, "after-drop")
            log("DONE. Check the screenshots folder and the browser window.")
        except KeyboardInterrupt:
            log("stopped (Ctrl+C)")
        except Exception as e:
            log(f"!!! STOPPED: {e}")
            notify("Registration script STOPPED", str(e)[:300], "urgent", "rotating_light")
            try:
                shot(page, "error")
            except Exception:
                pass
        flush_notifications()
        if not args.yes:
            try:
                input("\nPress Enter to close the browser...")
            except (EOFError, KeyboardInterrupt):
                pass
        try:
            ctx.close()
        except BaseException:
            pass


if __name__ == "__main__":
    main()
