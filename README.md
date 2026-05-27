# IW Ops Dashboard

Nightly ops briefing dashboard for Independent Waste. Rebuilds every weeknight at 7:00 PM CT via GitHub Actions → Claude API → Vercel. Slack notification sent on deploy.

## How it works

1. GitHub Actions cron fires at 00:00 UTC (7 PM CT) Mon–Fri
2. `scripts/build.py` calls Claude via Anthropic API with Supabase MCP attached
3. Claude queries the IW DB, generates a complete self-contained HTML dashboard
4. HTML is deployed to Vercel via their Deployments API
5. Slack webhook pings Ryan + Jack with a link to the updated dashboard

Supabase credentials never leave Claude — the build script only needs the Anthropic API key.

## Step-by-step setup

### Step 1 — Create a GitHub repo

1. Go to github.com → New repository
2. Name it `iw-ops-dashboard`, set to Private
3. Don't initialize with a README (we have one)
4. Copy the repo URL

Push this folder to it:
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_ORG/iw-ops-dashboard.git
git push -u origin main
```

### Step 2 — Get your Anthropic API key

1. Go to console.anthropic.com → API Keys
2. Create a new key named `iw-ops-dashboard`
3. Copy it — you'll add it to GitHub secrets in Step 5

### Step 3 — Set up Vercel

1. Go to vercel.com → Sign up or log in
2. Go to vercel.com/account/tokens
3. Click **Create Token** → name it `iw-ops-dashboard` → No expiration
4. Copy the token — you'll add it to GitHub secrets in Step 5

Note: No need to create a Vercel project manually — the build script creates it automatically on first deploy.

### Step 4 — Set up Slack incoming webhook

1. Go to api.slack.com/apps → Create New App → From scratch
2. Name: `IW Ops Dashboard` → choose your workspace
3. In the left menu → Incoming Webhooks → toggle On
4. Click **Add New Webhook to Workspace**
5. Choose the channel to post to (or create `#ops-briefing`)
6. Copy the webhook URL — you'll add it to GitHub secrets in Step 5

### Step 5 — Add GitHub secrets

In your GitHub repo:
→ Settings → Secrets and variables → Actions → New repository secret

Add these three:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | From Step 2 |
| `VERCEL_TOKEN` | From Step 3 |
| `SLACK_WEBHOOK_URL` | From Step 4 |

### Step 6 — Test the build

1. In your GitHub repo → Actions tab
2. Click **IW Ops Dashboard — Nightly Build**
3. Click **Run workflow** → Run workflow
4. Watch the logs — should complete in ~2 minutes
5. Check Vercel dashboard for the deploy URL
6. Check Slack for the notification

After a successful run the dashboard will be live at:
`https://iw-ops-dashboard.vercel.app`

## File structure

```
.github/
  workflows/
    nightly.yml        # GitHub Actions cron — runs at 7 PM CT Mon–Fri
scripts/
  build.py             # Calls Claude, deploys to Vercel, pings Slack
public/
  index.html           # Seeded dashboard (replaced nightly by build)
README.md
.gitignore
```

## Secrets summary

| Secret | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com/api-keys |
| `VERCEL_TOKEN` | vercel.com/account/tokens |
| `SLACK_WEBHOOK_URL` | api.slack.com/apps |
