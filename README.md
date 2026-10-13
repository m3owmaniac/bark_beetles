# registrator

This is a program that runs on **your computer**, not a website. It opens its own
browser window on Mt. SAC's registration site and clicks through for you:
**Register for Classes → Winter 2027 → Continue → Enter CRNs → Add to Summary →
Action: \*\*Web Registered\*\* → Submit**.

It does one class at a time, in priority order:

1. **CRN1**. If it's definitely unavailable (no Web Registered option, only
   Waitlist/Remove, or Banner shows an error on it after Submit), the script
   sets it to **Remove**, clicks Submit, then tries **CRN2**.
2. **CRNANOTHERCLASS**.

To change CRNs, edit `CLASSES` at the top of `register.py`.

## When the site is slow or overloaded

* A slow or broken site (timeouts, error pages, a click that does nothing,
  no answer after Submit) is **not** treated as "class full". The script
  reloads the Register for Classes page and tries the **same** CRN again.
  It only moves on to the backup when Banner actually says no.
* If the reload lands somewhere else (the term page or an error page), it
  selects Winter 2027 and clicks Continue again.
* It waits a little longer after each failed try (1 to 5 seconds), so it
  keeps trying without hammering the site. It keeps going for 30 minutes
  after 8:00.
* If Submit gets no answer, it reloads the page and **checks whether the
  class got registered anyway** before trying again, so it never
  double-submits blindly.
* It only says REGISTERED when Banner's summary shows the class as
  registered. At the end it reloads the page once more and re-checks
  every class, so the final RESULTS are Banner's word, not a guess.

## 1. Setup (Linux Mint, one time)

In a Terminal, inside the `registration` folder:

```
sudo apt install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

## 2. Test on Fall 2026 (really submits, then drops again)

Pick a time a few minutes from now and put it in `--at`:

```
.venv/bin/python register.py --term "Fall 2026" --crns CRNTEST1 CRNTEST2 --drop-after --at "2026-09-23 23:45:00"
```

Log in when the browser opens, press Enter in the terminal, and leave it.
At that time it runs exactly like 8 AM will: it registers 24475, then 24585,
prints RESULTS, and then (`--drop-after`) drops both again and re-checks
that they're gone.

⚠️ This test **really registers** you. Dropping right after is normally
harmless, but it's your call. `--drop-after` only works with `--term`
set to something other than Winter 2027, so it can never drop your real classes.

For a test with no Submit at all, use `--dry-run` instead of `--drop-after`.

## 3. The real run (before you leave for work)

```
.venv/bin/python register.py
```

1. Log in when the browser opens, then press Enter in the terminal.
2. It immediately checks that "Winter 2027" is in the term dropdown and
   tells you. Read that line before you leave.
3. **Leave everything open.** Minimizing is fine. Don't close the browser
   or the terminal. It reloads the page every 4 minutes to keep you logged
   in, and blocks sleep. If it's a laptop, keep it plugged in with the lid **open**.
4. At 8:00:00 it starts. When you get back, read the RESULTS block in the
   terminal. Screenshots of every step are in `screenshots/`.

## Testing without the real site

`mock/` has a fake copy of the Banner pages. `load` simulates heavy traffic:

```
.venv/bin/python mock/server.py 8765 20 30169 load   # opens in 20s, 30169 full, heavy load
.venv/bin/python register.py --base http://127.0.0.1:8765/StudentRegistrationSsb/ssb --at now --yes
```
