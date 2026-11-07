# Deploying to Render

This app needs to run **24/7** (the 5-minute poller must never sleep), so use an
**always-on paid instance** — not the free tier, which sleeps when idle and would
pause data collection.

## One-time prep

1. **Put the project on GitHub** (from this folder):
   ```bash
   git init
   git add .
   git commit -m "Live momentum dashboard"
   gh repo create live-momentum --private --source=. --push
   ```
   (or create an empty repo on github.com and `git remote add origin … && git push -u origin main`)

## Deploy on Render

### Option A — Blueprint (uses render.yaml, fewest clicks)
1. Go to **https://dashboard.render.com** → **New ▾ → Blueprint**.
2. Connect your GitHub and pick the `live-momentum` repo. Render reads `render.yaml`.
3. It will ask for the **`APISPORTS_KEY`** secret (because it's marked `sync: false`).
   Paste your API-Football key. Click **Apply**.
4. Wait for the build/deploy (~2–3 min). You get a URL like
   `https://live-momentum.onrender.com`. Done.

### Option B — Manual (New Web Service)
1. **New ▾ → Web Service** → connect the repo.
2. Settings:
   - **Runtime:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 1 --threads 8 --bind 0.0.0.0:$PORT --timeout 120`
   - **Instance type:** **Starter** (always-on). ⚠️ Not "Free".
3. **Environment → Add Environment Variable:**
   - `APISPORTS_KEY` = your API-Football key
   - `POLL_INTERVAL` = `300`  (optional)
   - `MAX_NONE_CHECKS` = `2`  (optional)
4. **Create Web Service.** Open the URL when it's live.

## After deploy

- Health check: `https://<your-app>.onrender.com/api/meta` should return JSON with a
  recent `last_poll` and `counts`.
- Updates: push to GitHub → Render auto-deploys. On each redeploy the in-memory
  state resets and repopulates within one poll (momentum needs one extra cycle).
- Logs: Render dashboard → your service → **Logs**.

## Important notes

- **One worker only.** The poller is a single background thread with in-memory
  state. Running multiple workers would double your API usage and split the data.
  (`render.yaml` and the start command already pin `--workers 1`.)
- **Cost:** Render Starter ≈ $7/month. The API-Football Ultra plan is separate
  (already purchased).
- **Secrets:** the key lives only in Render's env vars, never in the repo
  (`.gitignore` also blocks `.env`).
- **No database needed** — latest-only, in-memory, by design. (The archival
  system, when we build it, will add one.)
