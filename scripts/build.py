"""
IW Ops Dashboard — Nightly Build Script
1. Queries Supabase REST API directly for all haul data
2. Passes data to Claude API to generate HTML dashboard
3. Deploys to Vercel
4. Pings Slack
"""

import os
import sys
import json
import time
import requests
import anthropic
import zoneinfo
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
VERCEL_TOKEN        = os.environ["VERCEL_TOKEN"]
VERCEL_PROJECT      = "iw-ops-dashboard"
SLACK_WEBHOOK_URL   = os.environ["SLACK_WEBHOOK_URL"]
SUPABASE_URL        = "https://gusggxvsgnggxmgmmtve.supabase.co"
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service role for REST API
SUPABASE_ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]  # personal token for Management API
# Direct Postgres connection via Supabase session pooler (port 5432)
SUPABASE_PROJECT_REF = "gusggxvsgnggxmgmmtve"

MODEL       = "claude-sonnet-4-5"
OUTPUT_PATH = "public/index.html"
CT          = zoneinfo.ZoneInfo("America/Chicago")

TARGETS = {
    "BHM": {"min": 40, "max": 55},
    "BNA": {"min": 15, "max": 25},
    "DFW": {"min": 45, "max": 60},
    "SAT": {"min": 30, "max": 45},
    "HSV": {"min": 2,  "max": 5},
}

MARKET_IDS = {"BHM": 36, "BNA": 34, "DFW": 40, "SAT": 37, "HSV": 39}

NORMS = {
    "BHM": {"median_drivers": 8, "p25_drivers": 7, "p75_hauls_per": 5.0},
    "BNA": {"median_drivers": 3, "p25_drivers": 2, "p75_hauls_per": 7.0},
    "DFW": {"median_drivers": 10,"p25_drivers": 8, "p75_hauls_per": 5.3},
    "SAT": {"median_drivers": 5, "p25_drivers": 4, "p75_hauls_per": 5.7},
    "HSV": {"median_drivers": 1, "p25_drivers": 1, "p75_hauls_per": 6.8},
}

# ── Database connection ───────────────────────────────────────────────────────
def run_sql(sql: str) -> list:
    """Run SQL via Supabase Management API."""
    resp = requests.post(
        f"https://api.supabase.com/v1/projects/{SUPABASE_PROJECT_REF}/database/query",
        headers={
            "Authorization": f"Bearer {SUPABASE_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"query": sql},
        timeout=30,
    )
    if resp.ok:
        data = resp.json()
        # Response is either a list of rows or {"rows": [...]}
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        return data
    raise Exception(f"Query failed: {resp.status_code} {resp.text[:400]}")


# ── Fetch data ────────────────────────────────────────────────────────────────
def fetch_data() -> dict:
    today_ct = datetime.now(CT).date()
    month_start = today_ct.replace(day=1)
    trend_start = "2026-04-01"

    print(f"Fetching data for {today_ct} CT...")

    # Daily hauls per market from April 1 through today
    # Weekend hauls rolled into preceding Friday
    daily_sql = f"""
    WITH daily AS (
      SELECT
        m.name AS market,
        DATE(o.completed_at AT TIME ZONE 'America/Chicago') AS haul_date,
        EXTRACT(DOW FROM o.completed_at AT TIME ZONE 'America/Chicago') AS dow,
        COUNT(*) AS hauls
      FROM orders o
      JOIN projects p ON p.id = o.project_id
      JOIN markets m ON m.id = p.market_id
      WHERE o.deleted_at IS NULL
        AND (o.status IN ('completed','billed') OR (o.status = 'canceled' AND o.driver_id IS NOT NULL))
        AND o.completed_at AT TIME ZONE 'America/Chicago' >= '{trend_start}'
        AND o.completed_at AT TIME ZONE 'America/Chicago' < '{today_ct + timedelta(days=1)}'
        AND m.name IN ('BHM','BNA','DFW','SAT','HSV')
      GROUP BY 1, 2, 3
    )
    SELECT
      market,
      CASE
        WHEN dow = 0 THEN haul_date - INTERVAL '2 days'  -- Sunday → Friday
        WHEN dow = 6 THEN haul_date - INTERVAL '1 day'   -- Saturday → Friday
        ELSE haul_date
      END AS plot_date,
      SUM(hauls) AS productive_hauls
    FROM daily
    GROUP BY 1, 2
    ORDER BY 1, 2
    """

    # Today's summary per market
    today_sql = f"""
    SELECT
      m.name AS market,
      COUNT(*) FILTER (WHERE o.status IN ('completed','billed') OR (o.status = 'canceled' AND o.driver_id IS NOT NULL)) AS productive_today,
      COUNT(DISTINCT o.driver_id) FILTER (WHERE o.status IN ('completed','billed')) AS active_drivers,
      COUNT(*) FILTER (WHERE o.status IN ('available','assigned','progress','paused')
        OR (o.status IN ('completed','billed') AND DATE(o.completed_at AT TIME ZONE 'America/Chicago') = '{today_ct}')
        OR (o.status = 'canceled' AND o.driver_id IS NOT NULL AND DATE(o.completed_at AT TIME ZONE 'America/Chicago') = '{today_ct}')
      ) AS addressable_today
    FROM orders o
    JOIN projects p ON p.id = o.project_id
    JOIN markets m ON m.id = p.market_id
    WHERE o.deleted_at IS NULL
      AND m.name IN ('BHM','BNA','DFW','SAT','HSV')
      AND (
        (DATE(o.completed_at AT TIME ZONE 'America/Chicago') = '{today_ct}')
        OR (o.status IN ('available','assigned','progress','paused') AND o.started_at <= NOW())
      )
    GROUP BY m.name
    """

    # MTD avg per market
    mtd_sql = f"""
    SELECT
      m.name AS market,
      ROUND(AVG(daily_hauls)::numeric, 1) AS mtd_avg
    FROM (
      SELECT
        m.name,
        DATE(o.completed_at AT TIME ZONE 'America/Chicago') AS day,
        COUNT(*) AS daily_hauls
      FROM orders o
      JOIN projects p ON p.id = o.project_id
      JOIN markets m ON m.id = p.market_id
      WHERE o.deleted_at IS NULL
        AND (o.status IN ('completed','billed') OR (o.status = 'canceled' AND o.driver_id IS NOT NULL))
        AND DATE(o.completed_at AT TIME ZONE 'America/Chicago') >= '{month_start}'
        AND DATE(o.completed_at AT TIME ZONE 'America/Chicago') <= '{today_ct}'
        AND EXTRACT(DOW FROM o.completed_at AT TIME ZONE 'America/Chicago') NOT IN (0,6)
        AND m.name IN ('BHM','BNA','DFW','SAT','HSV')
      GROUP BY 1, 2
    ) d
    JOIN markets m ON m.name = d.name
    GROUP BY m.name
    """

    try:
        daily_rows  = run_sql(daily_sql)
        today_rows  = run_sql(today_sql)
        mtd_rows    = run_sql(mtd_sql)
    except Exception as e:
        print(f"DB query error: {e}")
        raise

    return {
        "today": str(today_ct),
        "daily_rows": daily_rows,
        "today_rows": today_rows,
        "mtd_rows": mtd_rows,
        "targets": TARGETS,
        "norms": NORMS,
    }


# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a dashboard generator for Independent Waste (IW), a roll-off waste hauler.

You will receive pre-fetched data as JSON. Generate a complete, self-contained HTML dashboard.
Output ONLY raw HTML starting with <!DOCTYPE html> — no explanation, no markdown fences.

IW Brand colors: navy #1b1c51, orange #f26a21, green #00a775, bone background #f5f3ef
Font: system-ui, sans-serif

The dashboard must include:
- Platform header: total productive hauls today across all markets, total addressable, MTD avg
- One card per market (DFW, BHM, SAT, BNA, HSV order): today hauls, addressable, MTD avg, target range, active drivers, verdict badge
- Action plan table: off-target markets ranked by MTD miss %, verdict, investigate note
- Footer: "IW Ops Dashboard · {date} · Data via IW DB"

VERDICT LOGIC (check in order, first that trips wins):
1. Sales gap: addressable_today < target_min
2. Driver gap: active_drivers <= p25_drivers from norms
3. Capacity ceiling: (productive_today / active_drivers) >= p75_hauls_per from norms
4. Throughput gap: default if board full and drivers present but still missing target
If on target: show green "On target" badge

CHART REQUIREMENTS:
- Use Chart.js from https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js
- All chart initialization inside window.addEventListener('load', ...) — never inline
- One mini chart per card, height 72px
- X-axis: business days only (Mon-Fri), no weekend dates unless hauls > 0
- Dataset order in Chart.js:
  [0] Target band top (target_max, fill '+1', backgroundColor 'rgba(0,167,117,0.13)', borderColor 'transparent', pointRadius 0)
  [1] Target band bottom (target_min, borderDash [3,3], borderColor 'rgba(0,167,117,0.35)', fill false, pointRadius 0)
  [2] Daily hauls line (borderColor '#d0ceca', borderWidth 1.5, fill false, pointRadius 0 except rain dots which are blue #4a90d9 pointRadius 4, spanGaps false)
  [3] 5-business-day rolling average (borderColor '#1b1c51', borderWidth 2.5, tension 0.4, fill false, pointRadius 0)
- Rolling average: for each index i, average of up to 5 preceding values including i
- Rain dots: fetch Open-Meteo live in browser for each market, mark days >= 2.54mm precipitation
  URL: https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lng}&daily=precipitation_sum&start_date=2026-04-01&end_date={today}&timezone=America/Chicago
  Coords: BHM(33.5779,-86.7514), BNA(36.1245,-86.6782), DFW(32.8998,-97.0403), SAT(29.5337,-98.4698), HSV(34.6372,-86.7750)

Bake all haul data into the HTML as JS constants. Open-Meteo fetched live from browser only.

Output ONLY the HTML. Nothing else."""


def build_user_prompt(data: dict) -> str:
    return f"""Today is {data['today']} (Central Time).

Here is the pre-fetched data from the IW database:

DAILY HAULS (April 1 through today, weekends rolled into preceding Friday):
{json.dumps(data['daily_rows'], indent=2)}

TODAY'S SUMMARY PER MARKET:
{json.dumps(data['today_rows'], indent=2)}

MTD AVERAGE PER MARKET:
{json.dumps(data['mtd_rows'], indent=2)}

TARGETS: {json.dumps(data['targets'])}

60-DAY TRAILING NORMS: {json.dumps(data['norms'])}

Generate the complete dashboard HTML now. Output ONLY raw HTML starting with <!DOCTYPE html>."""


# ── Call Claude ───────────────────────────────────────────────────────────────
def call_claude(data: dict) -> str:
    print("Calling Claude API...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=300.0)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_prompt(data)}],
            )
            break
        except anthropic.RateLimitError as e:
            if attempt < max_retries - 1:
                wait = 30 * (attempt + 1)
                print(f"Rate limit, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise e

    html_output = ""
    for block in response.content:
        if hasattr(block, "text"):
            html_output += block.text

    start = html_output.find("<!DOCTYPE")
    if start == -1:
        start = html_output.find("<html")
    if start != -1:
        html_output = html_output[start:]

    if not html_output.strip():
        raise ValueError("Claude returned empty output")

    print(f"Generated HTML: {len(html_output):,} chars")
    return html_output


# ── Write HTML ────────────────────────────────────────────────────────────────
def write_html(html: str):
    os.makedirs("public", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Written to {OUTPUT_PATH}")


# ── Deploy to Vercel ──────────────────────────────────────────────────────────
def get_static_url() -> str:
    resp = requests.get(
        f"https://api.vercel.com/v9/projects/{VERCEL_PROJECT}",
        headers={"Authorization": f"Bearer {VERCEL_TOKEN}"},
        timeout=30,
    )
    if resp.ok:
        data = resp.json()
        for a in data.get("alias", []):
            domain = a.get("domain", "")
            if domain.endswith(".vercel.app") and "preview" not in domain:
                return f"https://{domain}"
    return f"https://{VERCEL_PROJECT}.vercel.app"


def deploy_to_vercel(html: str) -> str:
    print("Deploying to Vercel...")
    static_url = get_static_url()
    print(f"Static URL: {static_url}")

    resp = requests.post(
        "https://api.vercel.com/v13/deployments",
        headers={
            "Authorization": f"Bearer {VERCEL_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "name": VERCEL_PROJECT,
            "files": [{"file": "index.html", "data": html, "encoding": "utf-8"}],
            "projectSettings": {"framework": None},
            "target": "production",
        },
        timeout=60,
    )

    if not resp.ok:
        print(f"Vercel error: {resp.status_code} — {resp.text[:300]}")
        resp.raise_for_status()

    print(f"Deploy created: {resp.json().get('url')}")
    return static_url


# ── Ping Slack ────────────────────────────────────────────────────────────────
def ping_slack(deploy_url: str, data: dict):
    print("Pinging Slack...")
    today_str = datetime.now(CT).strftime("%A %b %-d, %Y")

    total_today = sum(
        r.get("productive_today", 0) or 0
        for r in data["today_rows"]
    )
    total_addr = sum(
        r.get("addressable_today", 0) or 0
        for r in data["today_rows"]
    )

    payload = {
        "text": f"📦 IW Ops Dashboard updated — {today_str}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📦 IW Daily Ops Briefing — {today_str}", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Platform:* {total_today} productive hauls · {total_addr} addressable\n*Snapshot:* 7:00 PM CT · completed + dry hauls · data via IW DB",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Dashboard", "emoji": True},
                    "url": deploy_url,
                    "style": "primary",
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"IW Ops Tracker · Built with Claude · <{deploy_url}|Open dashboard>"}],
            },
        ],
    }

    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()
    print("Slack notification sent")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== IW Ops Dashboard Build — {datetime.now(CT).isoformat()} ===")
    try:
        data = fetch_data()
        html = call_claude(data)
        write_html(html)
        deploy_url = deploy_to_vercel(html)
        ping_slack(deploy_url, data)
        print(f"=== Build complete: {deploy_url} ===")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
