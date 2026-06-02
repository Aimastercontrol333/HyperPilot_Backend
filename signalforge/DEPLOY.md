# SignalForge — Deployment Guide (Phase 1 Backend)

**Written for a non-technical person. No prior coding knowledge needed.**
You will do two things:
- **Part 1** — Run the engine on your own Mac once, just to *see it work*. (15 min, zero risk.)
- **Part 2** — Make it run automatically every 30 minutes and publish its results to your website, for **free**, using GitHub + Vercel (the same tools you already used for the site).

There is nothing to break and nothing that costs money in this guide.

---

## What we are actually doing (plain English)

The backend is a small program. When it runs, it:
1. looks at real traders on Hyperliquid,
2. scores them with your Safety Score,
3. paper-trades the good ones with realistic costs,
4. writes the results into a single file called **`dashboard.json`**.

Your website then reads that file and shows the numbers. So the whole job is:
**"run the program on a schedule, and put `dashboard.json` where the website can see it."**

---

# PART 1 — Run it once on your Mac (to see it work)

### Step 1.1 — Install Python (the language the program is written in)

1. Open this link in your browser: **https://www.python.org/downloads/**
2. Click the big yellow button **"Download Python 3.x"**.
3. Open the file that downloads (it ends in `.pkg`) and click **Continue → Continue → Agree → Install**. Type your Mac password if asked.
4. When it says "Installation was successful," click **Close**.

### Step 1.2 — Open the Terminal

1. Press **Cmd (⌘) + Space** to open Spotlight search.
2. Type **Terminal** and press **Enter**. A small window with text appears. This is where you type commands.

> Tip: To paste into Terminal, use **Cmd + V**. To run a command, press **Enter**.

### Step 1.3 — Unzip the backend

1. Find the file I gave you: **`signalforge-phase1-backend.zip`** (probably in your **Downloads** folder).
2. **Double-click it.** A folder called **`signalforge`** appears next to it.
3. Drag that **`signalforge`** folder onto your **Desktop** so it's easy to find.

### Step 1.4 — Go into the folder in Terminal

In Terminal, type this exactly and press Enter:

```
cd ~/Desktop/signalforge
```

(That means "go into the signalforge folder on my Desktop." Nothing visible happens — that's normal.)

### Step 1.5 — Install the one thing the program needs

Type this and press Enter:

```
pip3 install -r requirements.txt
```

You'll see a few lines of text. When you get your prompt back, it's done.

### Step 1.6 — Run the self-test (no internet trading needed)

Type this and press Enter:

```
python3 smoke_test.py
```

**What you should see:** a few lines showing test wallets being scored — disciplined ones passing with a score around 70, a "gambler" getting **banned**, and a paper-sim result with a capacity curve. If you see **"✓ engine ran end-to-end"** at the bottom, **everything works.** 🎉

### Step 1.7 (optional) — Run it against the REAL Hyperliquid data once

This actually contacts Hyperliquid. It's read-only — it just looks at public data, it never trades or touches money.

```
python3 -m sf.pipeline --max 40 --out dashboard.json
```

Let it run (it may take a minute or two). When done, a file called **`dashboard.json`** appears inside the `signalforge` folder. Open it by typing:

```
open dashboard.json
```

You'll see real numbers (wallets audited, pass rate, basket stats). That's your engine working on live data.

> If it shows `wallets_audited: 0`, the public leaderboard list was empty or unreachable that moment — that's expected and is exactly why the **WebSocket harvester** (the next thing I'm building) matters: it finds traders directly from the live trade stream instead of relying on the leaderboard.

**Part 1 is done.** You've proven the engine runs. Now let's automate it.

---

# PART 2 — Run it automatically & feed your website (free)

We'll add the backend into the **same GitHub repository** that holds your website, and add a small "robot" (GitHub Actions) that runs the program every 30 minutes and saves `dashboard.json` into the repo. Vercel then serves that file automatically — no new accounts, no cost.

### Step 2.1 — Open your website's GitHub repository

1. Go to **https://github.com** and sign in.
2. Click your repository that contains your website (the one connected to Vercel — it has your `index.html` in it).

### Step 2.2 — Upload the backend folder

1. On the repo page, click **Add file ▸ Upload files**.
2. Open your Desktop, and drag the whole **`signalforge`** folder into the upload area. Wait for all files to finish uploading.
3. Scroll down, in the message box type: `add backend`, and click **Commit changes**.

You now have a `signalforge` folder inside your repo, next to your `index.html`.

### Step 2.3 — Add the auto-refresh robot

1. On the repo page, click **Add file ▸ Create new file**.
2. In the filename box at the top, type exactly:
   ```
   .github/workflows/refresh-dashboard.yml
   ```
   (As you type the `/` characters, GitHub will create the folders for you.)
3. Paste this **exactly** into the big text box:

```yaml
name: Refresh dashboard data
on:
  workflow_dispatch:          # adds a manual "Run" button
  schedule:
    - cron: '*/30 * * * *'    # also runs every 30 minutes
permissions:
  contents: write
jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        working-directory: signalforge
        run: pip install -r requirements.txt
      - name: Run the pipeline
        working-directory: signalforge
        run: python -m sf.pipeline --max 80 --out ../dashboard.json
      - name: Save dashboard.json back to the repo
        run: |
          git config user.name "signalforge-bot"
          git config user.email "bot@users.noreply.github.com"
          git add dashboard.json
          git diff --staged --quiet || git commit -m "chore: refresh dashboard data [skip ci]"
          git push
```

4. Scroll down and click **Commit new file**.

### Step 2.4 — Turn it on and run it once

1. Click the **Actions** tab at the top of your repo. If GitHub asks you to enable Actions, click the green **"I understand… enable"** button.
2. On the left, click **"Refresh dashboard data"**.
3. Click the **"Run workflow"** dropdown on the right, then the green **"Run workflow"** button.
4. Wait ~1 minute, refresh the page. A run appears with a spinning icon, then a **green check ✓** when finished.

### Step 2.5 — Confirm your website can see the data

After the green check, open this in your browser (replace with your real site address):

```
https://YOUR-SITE.vercel.app/dashboard.json
```

You should see the real numbers as text. **That means your website now has live data available.** From here on it refreshes by itself every 30 minutes.

> Want different/more wallets watched? In Step 2.3's file, change `--max 80` to a bigger number, or add `--seeds 0xADDRESS1,0xADDRESS2` after it to always include specific wallets.

---

## What's next (I'm building these now)

1. ~~**WebSocket harvester**~~ ✅ built (`sf/ingest/harvester.py`)
2. ~~**Persistence layer**~~ ✅ built (`sf/ingest/store.py`)
3. **Wiring the website** — replacing the placeholder numbers on your pages with the live values from `dashboard.json` (next).

---

# PART 3 — Run the always-on worker (full discovery)

Use this when you want the system to discover traders **automatically from the live
trade stream** (instead of only the leaderboard) and keep a memory of every wallet.
This needs a program that runs 24/7, so instead of GitHub Actions we use a tiny
always-on host. **Render** is the most beginner-friendly; the worker costs about
**$7/month** — trivial versus the project budget, and you can switch it off anytime.

### Step 3.1 — Create a Render account
1. Go to **https://render.com** and click **Get Started** → sign in **with GitHub**
   (so it can see your repo). Approve access.

### Step 3.2 — Create the worker
1. Click **New ▸ Web Service**.
2. Pick your repository (the one with the `signalforge` folder).
3. Fill in these boxes exactly:
   - **Root Directory:** `signalforge`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python -m sf.worker`
   - **Instance Type:** the cheapest paid one (free instances sleep, which would
     stop the harvester).
4. Click **Advanced ▸ Add Environment Variable** and add (optional but recommended):
   - `SCORE_EVERY_MIN` = `20`
   - `SF_MAX_WALLETS` = `150`
5. Click **Create Web Service**. Render builds and starts it (takes a few minutes).

### Step 3.3 — Confirm it's alive
1. When it says **Live**, Render shows a URL like `https://signalforge-xxxx.onrender.com`.
2. Open `https://signalforge-xxxx.onrender.com/health` in your browser. You'll see
   a status with how many wallets have been **discovered** and **scored** so far.
   (Discovered climbs within seconds; scored fills in after the first scoring pass.)
3. Open `https://signalforge-xxxx.onrender.com/dashboard.json` to see the live data.

### Step 3.4 — Point the website at it
The site just needs to fetch that URL. I'll wire this into your pages next, but if
you want to test now, your dashboard URL is the one from Step 3.3 + `/dashboard.json`.

> The worker keeps discovering and re-scoring on its own. Leave it running. The
> longer it runs, the more wallets it has seen and the richer the audit universe.

---

## If something goes wrong

- **`pip3: command not found`** → Python didn't install. Redo Step 1.1, and restart Terminal.
- **`python3: command not found`** → same fix; make sure you clicked "Install" all the way through.
- **The Actions run shows a red ✗** → click into it, click the failed step to read the message, and send me a screenshot. The most common cause is a typo in the `.yml` file — re-paste it exactly.
- **`dashboard.json` shows `wallets_audited: 0`** → normal for now; the harvester I'm building fixes discovery.
- **Nothing is broken if you stop here.** None of this touches money, deposits, or trading. It only reads public data.
