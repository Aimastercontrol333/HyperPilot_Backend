# Get SignalForge Live — Detailed Guide (no coding, browser only)

You will do everything by clicking in your web browser. No Terminal. No commands.
Total time: about 30–40 minutes, most of it waiting for things to build.

---

## The mental model (read this once)

Your backend is a program that must run **24/7**, because it is always listening to
Hyperliquid's live trade feed and mirroring trades the instant they happen. A laptop
can't do that (you'd have to leave it on forever). So:

- **GitHub** = online storage for the code (like Google Drive for programs).
- **Render** = a cloud computer that runs the code non-stop (~$7/month).
- **Vercel** = where your website already lives; it will read the live data.

The flow once it's running:

```
Render computer runs your program  ─►  produces live data at a web address
                                         (e.g. https://signalforge.onrender.com/dashboard.json)
                                                        │
                                          your website reads from that address
```

You do **not** need to set up a database or get any Hyperliquid key — the program
creates its own database automatically and reads Hyperliquid's public data freely.

---

# PART 1 — Put the code on GitHub

**What's happening:** we upload your `signalforge` folder to an online locker so the
cloud computer can read it.

### 1.1  Make a GitHub account (skip if you have one)
1. Go to **https://github.com** → click **Sign up**.
2. Follow the prompts (email, password, username). Verify your email.

### 1.2  Unzip the file you downloaded
1. Find **`signalforge-phase1-backend.zip`** in your Downloads.
2. **Double-click it.** You now have a folder called **`signalforge`**.
3. Drag that **`signalforge`** folder to your **Desktop** so it's easy to find.

### 1.3  Create an empty storage locker (a "repository")
1. On GitHub, click the **+** at the top-right → **New repository**.
2. **Repository name:** type `signalforge-backend`.
3. Leave everything else as-is. Make sure **Public** is selected (it's free and fine —
   this is just code, no secrets in it).
4. Click **Create repository**.

### 1.4  Upload your code into the locker
1. On the new repository page, click the link **"uploading an existing file"**
   (it's in the middle of the page).
2. Open your **Desktop**, and **drag the whole `signalforge` folder** into the
   big upload box on the GitHub page. Wait until every file shows up in the list
   (about 20 files — give it a minute).
3. Scroll to the bottom and click the green **Commit changes** button.

> **What you should see:** your repository page now shows a `signalforge` folder.
> Click into it — you should see `README.md`, `requirements.txt`, and an `sf` folder.
> If you see those, Part 1 is done. ✅

---

# PART 2 — Run the code on Render (the always-on computer)

**What's happening:** we tell Render to grab your code from GitHub, install what it
needs, and run it forever.

### 2.1  Make a Render account
1. Go to **https://render.com** → click **Get Started**.
2. Choose **Sign in with GitHub** (this lets Render see your code). Click
   **Authorize** when GitHub asks.

### 2.2  Create the service
1. In Render, click **New** (top right) → **Web Service**.
2. You'll see your GitHub repositories. Find **`signalforge-backend`** and click
   **Connect**. (If you don't see it, click "Configure account" and give Render
   access to that repo.)

### 2.3  Fill in the settings — copy these EXACTLY

| Field | What to put |
|---|---|
| **Name** | `signalforge` |
| **Region** | pick the one closest to you |
| **Branch** | `main` |
| **Root Directory** | `signalforge` |
| **Runtime / Language** | `Python 3` (Render usually auto-detects this) |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python -m sf.worker` |
| **Instance Type** | **Starter** (~$7/mo). **Do NOT pick Free** — free machines fall asleep, which stops the live trade feed. |

> **What these mean, in plain words:**
> - **Root Directory** tells Render the code is inside the `signalforge` folder.
> - **Build Command** = "install the two small tools the program needs."
> - **Start Command** = "now run the program." (`sf.worker` is the always-on program
>   that runs the harvester, the scorer, and the live copy-trading all at once.)

### 2.4  Add a couple of settings (optional but recommended)
1. Scroll to **Environment Variables** → click **Add Environment Variable**, twice:
   - Key: `SCORE_EVERY_MIN`  Value: `20`   (re-scores wallets every 20 minutes)
   - Key: `SF_MAX_WALLETS`    Value: `150`  (how many wallets to audit per pass)

### 2.5  Set the health check (so Render knows it's alive)
1. Scroll to **Health Check Path** → type `/health`.
   (This is the address Render pings to confirm your program is running.)

### 2.6  Launch it
1. Click **Create Web Service**.
2. Render now downloads your code, installs the tools, and starts it. You'll see a
   live log scrolling. This takes **3–5 minutes**. When you see **"Live"** with a
   green dot at the top, it's running. ✅

### 2.7  Add a persistent disk (so it remembers across restarts)
By default the program forgets everything (discovered wallets, scores, the live
equity curve) whenever Render restarts. A small disk fixes that — it costs about
$1/month.
1. Render → your service → **Disks** (left menu) → **Add Disk**:
   - **Name:** `data`
   - **Mount Path:** `/var/data`
   - **Size:** `1` (GB)
   - Save.
2. Render → your service → **Environment** → add these four variables (this tells
   the program to write its memory onto the disk):
   - `SF_DB`    = `/var/data/signalforge.db`
   - `SF_OUT`   = `/var/data/dashboard.json`
   - `SF_LIVE`  = `/var/data/live_paper.json`
   - `SF_STATE` = `/var/data/live_state.json`
   - `SF_WF`    = `/var/data/walkforward.json`
3. Click **Save Changes**. Render redeploys (~2 min). From now on, the discovered
   wallets, the Safety Scores, and the **live equity curve all survive restarts and
   redeploys** — your track record keeps building instead of resetting.

### Reading the "does the score work?" verdict
Your backend runs the **walk-forward test** automatically (a few minutes after
startup, then once a day) and serves the result here:
```
https://hyperpilot-backend.onrender.com/walkforward.json
```
Look at the **`plain_english`** line — it tells you in one sentence whether high
Safety Scores actually predicted better forward performance. Early on it will say
"not enough audited wallets yet"; that's expected. It becomes a trustworthy read
once ~120 wallets have been audited (a few days of running).

---

# PART 3 — Check that it's actually working

Render gives your service a web address near the top, like
`https://signalforge.onrender.com`. Use it below (replace with your real one).

### 3.1  Is it alive?
Open in your browser:
```
https://signalforge.onrender.com/health
```
**You should see** something like:
`{"status":"ok", ... "db":{"discovered": 38, "scored": 0, "eligible": 0}}`

- **discovered** climbs within seconds — that's the harvester finding live traders. 🎉
- **scored** fills in after the first scoring pass (a couple of minutes).
- **eligible** = how many passed your Safety Score and made the basket.

### 3.2  See the data
Open:
```
https://signalforge.onrender.com/dashboard.json
```
You'll see the full live data as text — wallets audited, the basket, and a `"live"`
section that fills with open paper positions as your basket wallets start trading.

> **Patience note:** the `live` section only shows positions once (a) the first
> scoring pass has chosen a basket, AND (b) one of those basket wallets actually
> opens a trade. Early on it may say `"warming_up"` — that's normal. Leave it
> running; the longer it runs, the richer it gets.

---

# PART 4 — Connect it to your website (next step)

Your website just needs to read from your Render address. This is a small change to
the site's code (telling the pages to fetch `https://signalforge.onrender.com/dashboard.json`
instead of showing placeholder numbers). Ask me to wire this in and I'll do it — it's
quick, and then your live pages show real data.

---

## Money, stopping, and good-to-knows

- **Cost:** the Starter instance is about **$7/month**. You can pause or delete the
  service anytime in Render (Settings → Suspend / Delete). Nothing else costs money.
- **Stopping it:** Render → your service → **Settings** → **Suspend Web Service**.
  Resume anytime.
- **It forgets when it restarts:** by default the database resets if Render restarts
  or you redeploy. That's fine for now — the harvester re-discovers wallets in
  minutes. If you later want it to remember across restarts, Render has a "Disk"
  add-on (~$1/mo); ask me and I'll tell you the two settings to change.
- **Nothing here touches money or trades.** It only reads public data and simulates.
  No deposits, no real orders, no wallet keys anywhere.

---

## If something goes wrong

- **Render shows "Deploy failed" (red):** click the **Logs** tab, scroll to the
  bottom, and read the last red lines. The usual cause is a wrong setting in 2.3 —
  re-check **Root Directory** is `signalforge` and the **Start Command** is exactly
  `python -m sf.worker`. Screenshot the red lines and send them to me.
- **`/health` won't load:** wait 2 more minutes (it may still be building); refresh.
  If it still fails, check the Logs tab for red text.
- **discovered stays at 0:** the trade feed connection may have dropped; Render will
  reconnect automatically. Refresh `/health` after a minute.
- **You're stuck on any click:** tell me which Part and step number, and what you see
  on screen. I'll get you unstuck.

---

## (Optional) Want to see it run on your own Mac first?

Not required — but if you're curious and don't mind opening Terminal once, the
`DEPLOY.md` file (Part 1) walks through running the offline self-test on your laptop.
It's the same engine, just proving it works locally. Skip it if you'd rather go
straight to the cloud — the steps above are the real deployment.
