"""
IW Ops Dashboard — Nightly Build Script
Calls Claude via Anthropic API with Supabase MCP attached to:
  1. Query the IW DB for today's haul metrics
  2. Generate a complete self-contained HTML dashboard
Then deploys to Vercel and pings Slack.
"""

import os
import sys
import json
import requests
import anthropic
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
VERCEL_TOKEN      = os.environ["VERCEL_TOKEN"]
VERCEL_PROJECT    = "iw-ops-dashboard"
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

SUPABASE_MCP_URL      = "https://mcp.supabase.com/mcp"
SUPABASE_ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]
MODEL             = "claude-sonnet-4-5"
OUTPUT_PATH       = "public/index.html"

# ── Prompt ────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a data pipeline that generates an HTML ops dashboard for Independent Waste (IW).

You have access to the IW Supabase database via MCP tools. Query it to get today's metrics,
then output ONLY a complete, self-contained HTML file — no explanation, no markdown fences,
just raw HTML starting with <!DOCTYPE html>.

Key schema facts:
- Market IDs: BHM=36, BNA=34, DFW=40, SAT=37, HSV=39
- Always join orders → projects → markets (do not filter on orders.market_id directly)
- Soft deletes: filter deleted_at IS NULL
- Timestamps are UTC; convert to America/Chicago for all date logic
- Productive hauls: status='completed' OR status='billed' OR (status='canceled' AND driver_id IS NOT NULL)
- Active drivers: COUNT(DISTINCT driver_id) on completed orders today
- Addressable today: orders where started_at <= NOW() AND (still open OR completed/billed/dry-run today)

Targets: BHM 40-55, BNA 15-25, DFW 45-60, SAT 30-45, HSV 2-5

Verdict logic (check in order, use first that trips):
1. Sales gap: addressable_today < target_min
2. Driver gap: active_drivers_today <= p25_drivers_norm (BHM<=7, BNA<=2, DFW<=8, SAT<=4, HSV<=1)
3. Capacity ceiling: hauls_per_driver >= p75_norm (BHM>=5.0, BNA>=7.0, DFW>=5.3, SAT>=5.7, HSV>=6.8)
4. Throughput gap: default

Trailing 60-day norms: BHM median 8 drivers, BNA 3, DFW 10, SAT 5, HSV 1

IW Brand: navy #1b1c51, orange #f26a21, green #00a775, bone background #f5f3ef

The dashboard must include:
- Platform header: total productive hauls today, total addressable, May MTD avg
- One card per market: today's hauls, addressable, MTD avg, target, active drivers, verdict badge, mini trend chart (Chart.js CDN)
- Action plan table: off-target markets ranked by MTD miss %, with verdict and investigate note
- Footer with today's date

CHART REQUIREMENTS - follow exactly:
1. WEEKENDS: exclude weekend dates from the chart entirely UNLESS productive_hauls > 0 on that day.
2. TWO LINE COLORS: April data uses color #b4b2a9 (gray). May MTD data uses color #1b1c51 (navy). These must be separate Chart.js datasets on the same chart.
3. TARGET BAND: render as a filled green band between target_min and target_max. Use backgroundColor 'rgba(0,167,117,0.13)' for the fill. Use a dashed borderColor 'rgba(0,167,117,0.4)' on the min line. This must appear as a visible shaded region.
4. RAIN DOTS: fetch live from Open-Meteo archive API in the browser. Show as blue (#4a90d9) filled circle points with pointRadius 4 on days where precipitation_sum >= 2.54mm. All other points have pointRadius 0.
   API: https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lng}&daily=precipitation_sum&start_date=2026-04-01&end_date={today}&timezone=America/Chicago
   Coords: BHM(33.5779,-86.7514), BNA(36.1245,-86.6782), DFW(32.8998,-97.0403), SAT(29.5337,-98.4698), HSV(34.6372,-86.7750)

Chart.js dataset order must be:
  [0] band top line (target_max values, fill '+1' to next dataset, backgroundColor 'rgba(0,167,117,0.13)', borderColor 'transparent', pointRadius 0)
  [1] band bottom line (target_min values, borderColor 'rgba(0,167,117,0.4)', borderDash [3,3], fill false, pointRadius 0)
  [2] April haul data (borderColor '#b4b2a9', borderWidth 1.5, fill false, pointRadius per rain logic, spanGaps false)
  [3] May MTD haul data (borderColor '#1b1c51', borderWidth 2, fill false, pointRadius per rain logic, spanGaps false)

April + May MTD daily haul data must be queried from the DB and baked into the HTML as a JS const.
Open-Meteo is fetched live from the browser - no DB calls from the browser.

Output ONLY the HTML. Nothing else."""


def build_user_prompt():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""Today is {today}.

Please:
1. Query the IW database for:
   - Today's productive hauls, addressable capacity, and active drivers per market
   - Daily productive hauls per market from April 1 through today (for trend charts)
   - May MTD average per market

2. Generate the complete dashboard HTML with all data baked in.

Output ONLY the raw HTML file, starting with <!DOCTYPE html>."""


# ── Call Claude ───────────────────────────────────────────────────────────────
def call_claude() -> str:
    print("Calling Claude API with Supabase MCP...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt()}],
        mcp_servers=[
            {
                "type": "url",
                "authorization_token": SUPABASE_ACCESS_TOKEN,
                "url": SUPABASE_MCP_URL,
                "name": "iw-db",
            }
        ],
        betas=["mcp-client-2025-04-04"],
    )

    html_output = ""
    for block in response.content:
        if hasattr(block, "text"):
            html_output += block.text

    # Strip any accidental markdown fences
    start = html_output.find("<!DOCTYPE")
    if start == -1:
        start = html_output.find("<html")
    if start != -1:
        html_output = html_output[start:]

    if not html_output.strip():
        raise ValueError("Claude returned empty HTML output")

    print(f"Generated HTML: {len(html_output):,} chars")
    return html_output


# ── Write HTML ────────────────────────────────────────────────────────────────
def write_html(html: str):
    os.makedirs("public", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Written to {OUTPUT_PATH}")


# ── Deploy to Vercel ──────────────────────────────────────────────────────────
def deploy_to_vercel(html: str) -> str:
    print("Deploying to Vercel...")

    payload = {
        "name": VERCEL_PROJECT,
        "files": [
            {
                "file": "index.html",
                "data": html,
                "encoding": "utf-8"
            }
        ],
        "projectSettings": {
            "framework": None,
            "outputDirectory": None,
            "buildCommand": None,
            "installCommand": None
        },
        "target": "production"
    }

    resp = requests.post(
        "https://api.vercel.com/v13/deployments",
        headers={
            "Authorization": f"Bearer {VERCEL_TOKEN}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )

    if not resp.ok:
        print(f"Vercel error: {resp.status_code} — {resp.text}")
        resp.raise_for_status()

    deploy = resp.json()
    url = deploy.get("url", "")
    deploy_url = f"https://{url}" if url and not url.startswith("http") else url
    print(f"Deployed: {deploy_url}")
    return deploy_url


# ── Ping Slack ────────────────────────────────────────────────────────────────
def ping_slack(deploy_url: str):
    print("Pinging Slack...")
    today_str = datetime.now(timezone.utc).strftime("%A %b %-d, %Y")

    payload = {
        "text": f"📦 IW Ops Dashboard updated — {today_str}",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📦 IW Daily Ops Briefing — {today_str}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Tonight's dashboard has been updated with end-of-day data.\n*Snapshot:* 7:00 PM CT · completed + dry hauls · data via IW DB"
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Dashboard", "emoji": True},
                    "url": deploy_url,
                    "style": "primary"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"IW Ops Tracker · Built with Claude · <{deploy_url}|Open dashboard>"
                    }
                ]
            }
        ]
    }

    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    resp.raise_for_status()
    print("Slack notification sent")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== IW Ops Dashboard Build — {datetime.now(timezone.utc).isoformat()} ===")
    try:
        html = call_claude()
        write_html(html)
        deploy_url = deploy_to_vercel(html)
        ping_slack(deploy_url)
        print(f"=== Build complete: {deploy_url} ===")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
