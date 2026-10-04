# Account setup

Order of need (local-first, AM8):

| Service | Needed at | Free tier fits? |
|---|---|---|
| GitHub (repo + Actions) | M0 (now) | Yes, about 300–600 of 2,000 private-repo minutes a month |
| Telegram bot | M0 (now) | Yes, free |
| Neon Postgres | M9/M11 (cloud jobs and app) | Yes, under 100 MB for years |
| Cloudflare R2 | M9/M11 | Yes, if kept under 10 GB (we keep about 2–3 GB in the cloud) |
| Streamlit Community Cloud | M11 | Yes, with about 1 GB RAM; the app reads small precomputed tables only |

Rules for every secret: put it in `.env` locally (copy `.env.example`), in GitHub secrets for
Actions, and in Streamlit secrets for the app. Never paste it into code, chat, an issue, or a commit.
If one leaks, rotate it (each section says how).

Web UIs change. If a button named here has moved, the concept is the same; look for it nearby.

---

## 0. GitHub

1. Log the CLI in (opens a browser; choose HTTPS and "Login with a web browser"):
   ```bash
   gh auth login
   ```
2. Let git use those credentials:
   ```bash
   gh auth setup-git
   ```
3. Once secrets exist (below), add each one to the repo. `gh` prompts for the value, so it never
   lands in shell history:
   ```bash
   gh secret set TELEGRAM_TOKEN --repo varshith57/stock-prediction-tool
   ```
   Repeat for `TELEGRAM_CHAT_ID` now, and later for `DATABASE_URL`, `R2_ACCOUNT_ID`,
   `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`.

---

## 1. Telegram bot (needed now)

What the bot does here: it **only sends** you messages. The weekly plan is ready (once a week),
exit or hard-rule alerts on your holdings, and data-failure warnings, capped at 3 a week. It never
reads your messages and never takes commands; the one-tap Done/Partly/Skipped log lives in the app.
Messages contain tickers and counts only. The code refuses to send any text containing a rupee
amount, so holdings values can't leak into the chat.

### Create the bot
1. Install Telegram on your phone (and desktop if you like) and sign in.
2. Search for **@BotFather**. Pick the one with the blue verified tick; impostors exist.
3. Send `/newbot`.
   - **Name** (shown in chats): e.g. `Stockapp Alerts`.
   - **Username** (unique, must end in `bot`): e.g. `varshith_stockapp_bot`.
4. BotFather replies with a **token** like `123456789:AA...`. That token is full control of the
   bot. Treat it like a password.
5. Lock the bot down (still in BotFather):
   - `/setjoingroups`, pick your bot, choose **Disable**, so nobody can add it to a group.
   - Optional: `/setuserpic` and `/setdescription` ("Personal alerts. Not investment advice.").

### Get your chat ID
A bot can't message you until you've messaged it first.
1. Open your bot (search its username), tap **Start**, and send any message, e.g. `hi`.
2. Open `.env` in the project folder (create it with `cp .env.example .env` if it's missing) and
   paste the token after `TELEGRAM_TOKEN=` (no quotes, no spaces). Save.
3. In a terminal opened in the project folder, run:
   ```bash
   uv run stockapp telegram-chat-id
   ```
   It prints a line like `TELEGRAM_CHAT_ID=512345678`. Paste that into `.env`. If it says no chats
   were found, send the bot another message and rerun.

### Wire it up
1. Check that `.env` has both `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` filled in.
2. Send a test message:
   ```bash
   uv run stockapp telegram-test
   ```
   You should get "stockapp test alert (local) …" in Telegram.
3. Add both values to GitHub secrets (section 0, step 3). The next push to `main` makes CI send
   "stockapp test alert (ci)". That's the M0 gate.

**If the token leaks:** BotFather, `/revoke`, pick the bot, then update `.env` and the GitHub secret.

---

## 2. Neon Postgres (needed at M9/M11)

Holds app state only: holdings, signals, decisions, outcomes, config versions, job runs.
Locally we use Docker (`docker compose up -d db`) with the same schema.

1. Sign up at neon.tech (signing in with GitHub is fine).
2. **Create project**: name `stockapp`. Postgres version **16** (matches local).
   Region: **AWS Asia Pacific (Singapore)**, the closest to India and to IST job times.
3. **Dashboard → Connect**: copy the connection string. It looks like
   `postgresql://<user>:<password>@<host>.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   Use the **pooled** connection string for the app.
4. Store it as `DATABASE_URL` in GitHub secrets and Streamlit secrets. Keep `.env` pointing at local
   Docker during development.
5. At M11 I'll give you SQL to create a separate **read-only role** for the Streamlit app (PRD: the
   app reads, only jobs write). You'll run it in Neon's SQL editor.

Free-tier behaviour: the database sleeps when idle and wakes in about a second on the next query.
That's fine for this app.
**If the password leaks:** Neon, Roles, reset password, then update the secrets.

---

## 3. Cloudflare R2 (needed at M9/M11)

Holds the curated Parquet lake for cloud jobs and the app. Raw archives stay on your Mac.

1. Create a Cloudflare account at dash.cloudflare.com.
2. Sidebar: **R2 Object Storage**. Enabling R2 asks for a payment method even on the free plan.
   Free allowance: 10 GB-month storage, 1M write ops and 10M read ops a month, and **no egress
   fees**. We keep about 2–3 GB in the cloud, and the sync job will refuse to upload past 9 GB so
   you never cross into paid usage by accident.
3. **Create bucket**: name `stockapp-lake`, location hint **Asia-Pacific**. Leave public access
   **off**. Never enable an r2.dev public URL or a custom domain for this bucket.
4. **Manage R2 API tokens → Create API token**, twice:
   - `stockapp-jobs`: permission **Object Read & Write**, scoped to `stockapp-lake` only.
     Used by your Mac and GitHub Actions.
   - `stockapp-app`: permission **Object Read only**, scoped to `stockapp-lake` only.
     Used by Streamlit.
5. Each token shows an **Access Key ID** and a **Secret Access Key** once. Copy them then. Also note
   your **Account ID** (on the R2 overview page). The S3 endpoint is
   `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
6. Secrets: the jobs token goes to `.env` (only when we switch to R2) and GitHub secrets; the app
   token goes to Streamlit secrets.

**If a key leaks:** delete that API token in R2 and create a new one.

---

## 4. Streamlit Community Cloud (needed at M11)

1. Go to share.streamlit.io and sign in with GitHub. When asked, grant access to **private repos**
   (the repo is private).
2. At M11: **Create app**, repo `varshith57/stock-prediction-tool`, branch `main`, main file path
   (I'll give it then). Under **Advanced settings**, pick Python **3.12** and paste the secrets in
   TOML form (I'll provide the template).
3. Make it private: **App settings → Sharing**, set to only specific people, and add your own email.
   Free-tier limits on private apps have changed over time; check what the page offers when you
   deploy. The app will also have its own login (decided at M4), so it stays protected either way.
4. Free apps sleep after a period without visitors and wake in a few seconds when opened.
