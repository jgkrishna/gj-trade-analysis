# Deploying this dashboard to gjtradeanalysis.com

This walks through taking the dashboard from `localhost` to a real, always-on
site at your own domain. The files that make this possible are already in
this folder: `Dockerfile`, `requirements.txt`, `render.yaml`, `.gitignore`,
`.dockerignore`. You only need to do the account/payment steps below --
nothing here requires editing code.

Total ongoing cost: **domain (~$10-15/yr) + Render Starter plan (~$7/mo)**.
Optional add-ons below (persistent disk, cron job for alerts) cost a little more.

---

## Want to check it on your phone right now, before deploying anything?

You already can. `python run_dashboard.py` starts the server bound to all
network interfaces, not just localhost -- when it starts you'll see both:
```
Local URL:   http://localhost:8501
Network URL: http://<your-computer's-LAN-IP>:8501
```
On your phone, connected to the **same WiFi network**, open that Network URL
in a browser. If it doesn't load, Windows Firewall may be prompting (usually
silently, check for a popup) to allow Python through on first connection --
allow it. This is exactly the same responsive layout the deployed site will
use, so it's a real preview of the mobile experience, not a placeholder.

---

## 0. Password is OPTIONAL -- the site is public by design

This dashboard shows only public market data and generic backtest stats,
nothing personal, so the default is **no password at all**: if none is
configured anywhere, it renders normally for anyone with the link. This is
a deliberate choice, not a placeholder step to fill in.

**If you deployed earlier and set `DASHBOARD_PASSWORD_HASH` on Render:**
that variable is still there and will keep gating the live site even after
this update, since a configured hash always re-enables the gate. To make
it actually public, go to Render -> your service -> **Environment** and
**delete** the `DASHBOARD_PASSWORD_HASH` variable, then let it redeploy.

**If you'd rather lock it back down** (now, or any time later): generate a
hash and set it either locally or on Render -- the gate reappears
automatically as soon as one is configured, no code changes needed.
```bash
cd spcx_reversal
python hash_password.py
```
prints a line like `password_hash = "scrypt$abc123...$def456..."`.
- **Locally:** copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
  (gitignored -- never committed or pushed) and paste that line in.
- **On Render:** service -> **Environment** -> add `DASHBOARD_PASSWORD_HASH`
  with just the hash value (no `password_hash =` prefix).
- These two are independent -- your local machine and the live site read
  from completely separate places, so setting/removing one has zero effect
  on the other.

**Never put a real password or a real hash into `.streamlit/secrets.toml.example`**
-- that file (unlike `secrets.toml`) is tracked by git and gets pushed to
GitHub. It should only ever contain the placeholder text.

## 1. Buy the domain

Register `gjtradeanalysis.com` at any registrar -- [Namecheap](https://www.namecheap.com),
[Porkbun](https://porkbun.com), or [Cloudflare Registrar](https://www.cloudflare.com/products/registrar/)
are all reputable and cheap (no markup at Cloudflare/Porkbun specifically).
The WHOIS check I ran came back with no registration data, which is a good
sign it's open, but the registrar's search box is the only fully
authoritative check -- if it's somehow taken, you'll find out immediately
when you search there, before paying anything.

## 2. Push this folder to GitHub

Render deploys by connecting to a git repo, so this folder needs to be one.

```bash
cd spcx_reversal
git init
git add .
git commit -m "Initial commit: reversal dashboard"
```

Then create a new repo on [github.com/new](https://github.com/new) (empty,
no README/license -- you already have files), and push:

```bash
git remote add origin https://github.com/<your-username>/gj-trade-analysis.git
git branch -M main
git push -u origin main
```

Private or public is your call -- private is safer since the sidebar
exposes your detection thresholds, though nothing sensitive/secret lives in
this code (no API keys are needed; yfinance is unauthenticated).

## 3. Create a Render account and deploy

1. Sign up at [render.com](https://render.com) (GitHub login is fastest).
2. **New +** -> **Blueprint** -> connect the repo you just pushed. Render
   will detect `render.yaml` automatically and configure the service from it.
   (If you'd rather click through manually instead of using the blueprint:
   **New +** -> **Web Service** -> select the repo -> Runtime: **Docker** --
   it'll find the `Dockerfile` on its own.)
3. Pick the **Starter** plan (~$7/mo) so the app stays on permanently --
   the free tier spins down after 15 minutes idle and takes ~30-50s to wake
   back up on the next visit, which is a bad first impression on your own
   domain.
4. Deploy. First build takes a few minutes (installing scipy/matplotlib from
   source-ish wheels). You'll get a working `https://gj-trade-analysis.onrender.com`
   URL as soon as it's live -- confirm the dashboard actually loads there
   before moving on to the domain step.

## 4. Point the domain at Render

1. In the Render dashboard: your service -> **Settings** -> **Custom Domains**
   -> **Add Custom Domain** -> enter `gjtradeanalysis.com` (and separately
   `www.gjtradeanalysis.com` if you want the www version too).
2. Render shows you the exact DNS records to add (typically a `CNAME` for
   `www` pointing at your `*.onrender.com` address, and an `A`/`ALIAS` record
   for the bare apex domain -- Render's UI gives you the precise values, use
   those over anything written here since they can change).
3. Go to your registrar's DNS settings for the domain and add exactly those
   records.
4. Wait for DNS propagation (usually minutes, occasionally up to a few
   hours) and for Render to auto-issue a free SSL certificate once it
   detects the records. The custom-domain panel in Render shows live status.

## 5. Verify

Visit `https://gjtradeanalysis.com` -- should load the dashboard over HTTPS
with a valid certificate, no `onrender.com` in the address bar. Open the
same URL on your phone (any network, not just home WiFi, now that it's a
real public site) to confirm the mobile view.

---

## Optional: make the persisted history log actually persistent

The "Persisted History Log" section on the dashboard writes to a local
SQLite file. Render's Starter plan filesystem is **ephemeral** -- it works
fine while the service is running, but resets on every redeploy or restart,
silently losing history (and, if alerts are on, causing already-seen
reversals to look "new" again once). Locally this isn't an issue at all.

To make it durable on Render: **Settings** -> **Disks** -> add a persistent
disk (small size is plenty, a few dollars/mo), mount it at e.g. `/data`, then
set an environment variable `HISTORY_DB_PATH=/data/reversal_history.db`. If
you don't do this, the feature still works, it just resets periodically --
fine if you mainly care about it locally.

## Optional: set up alerts (email / webhook on new reversals)

Two independent pieces, both optional and either one alone is enough:

**Channel config** (env vars on Render, or `.streamlit/secrets.toml` locally
-- same names either way):
- `ALERT_SMTP_HOST` / `ALERT_SMTP_PORT` / `ALERT_SMTP_USER` / `ALERT_SMTP_PASSWORD` / `ALERT_EMAIL_TO`
  -- for Gmail: host `smtp.gmail.com`, port `587`, and an
  [App Password](https://myaccount.google.com/apppasswords) (not your real
  Gmail password) for `ALERT_SMTP_PASSWORD`.
- `ALERT_WEBHOOK_URL` -- any URL that accepts a JSON POST: a Slack incoming
  webhook, a Discord webhook, or a free service like [ntfy.sh](https://ntfy.sh).

**Trigger mechanism** -- pick one:
1. **In-app** (free, already built in): check "Email/webhook me when a NEW
   reversal is confirmed" in the sidebar. Only checks while the page is
   open/reloading, so you won't get notified while nobody's looking --
   fine if you check the dashboard daily anyway.
2. **Scheduled, hands-off** -- run `check_alerts.py` on a timer instead of
   relying on page views:
   - **Locally**: Windows Task Scheduler, a Basic Task running
     `python C:\path\to\spcx_reversal\check_alerts.py` on whatever interval
     you want. Only fires while your machine is on.
   - **On Render**: add a second service, type **Cron Job**, same repo/image,
     command `python check_alerts.py`, on a cron schedule. Small extra cost,
     but reliable since it doesn't depend on your laptop being on. Set
     `ALERT_WATCHLIST` (comma-separated tickers) as an env var on this service.

---

## Updating the live site later

Any `git push` to `main` auto-redeploys (Render watches the branch by
default). Local workflow stays exactly as it is now for day-to-day use
(`python run_dashboard.py`); only push when you want the public site updated.

## Password-protection notes

- The password check lives in `dashboard.py` (`require_password()`), using a
  timing-safe comparison (`hmac.compare_digest`) -- not just a plain `==`.
- It's a single shared password, not per-user accounts -- fine for "keep
  strangers out," not meant as real multi-user auth.
- Streamlit's session state means once you're signed in for a browser tab,
  you stay signed in until you close it or the server restarts -- no
  re-entering the password on every page interaction.
- If you ever need to force everyone out (e.g. after changing the password),
  just redeploy/restart the service -- session state resets.

## If you'd rather use Railway instead of Render

Everything above is Docker-based, so it's portable: on [railway.app](https://railway.app),
**New Project** -> **Deploy from GitHub repo** -> it detects the `Dockerfile`
the same way. Custom domains: **Settings** -> **Networking** -> **Custom
Domain**, same DNS-record pattern. `render.yaml` is ignored by Railway (it
has its own optional `railway.json`, not required for a basic deploy).
