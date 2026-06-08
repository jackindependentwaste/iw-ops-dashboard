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

    # Weekly period stats — 4 periods (current week Mon–today + 3 prior full weeks)
    period_sql = f"""
    WITH periods AS (
      SELECT
        CASE
          WHEN DATE_TRUNC('week', gs::date) = DATE_TRUNC('week', '{today_ct}'::date) THEN 0
          WHEN DATE_TRUNC('week', gs::date) = DATE_TRUNC('week', '{today_ct}'::date) - INTERVAL '7 days' THEN 1
          WHEN DATE_TRUNC('week', gs::date) = DATE_TRUNC('week', '{today_ct}'::date) - INTERVAL '14 days' THEN 2
          ELSE 3
        END AS period_num,
        gs::date AS day
      FROM generate_series(
        DATE_TRUNC('week', '{today_ct}'::date) - INTERVAL '21 days',
        '{today_ct}'::date,
        '1 day'::interval
      ) gs
      WHERE EXTRACT(DOW FROM gs) NOT IN (0,6)
    ),
    daily_hauls AS (
      SELECT
        m.name AS market,
        DATE(o.completed_at AT TIME ZONE 'America/Chicago') AS day,
        COUNT(*) AS hauls,
        COUNT(DISTINCT o.driver_id) AS drivers
      FROM orders o
      JOIN projects p ON p.id = o.project_id
      JOIN markets m ON m.id = p.market_id
      WHERE o.deleted_at IS NULL
        AND (o.status IN ('completed','billed') OR (o.status = 'canceled' AND o.driver_id IS NOT NULL))
        AND DATE(o.completed_at AT TIME ZONE 'America/Chicago') >= DATE_TRUNC('week', '{today_ct}'::date) - INTERVAL '21 days'
        AND DATE(o.completed_at AT TIME ZONE 'America/Chicago') <= '{today_ct}'
        AND EXTRACT(DOW FROM o.completed_at AT TIME ZONE 'America/Chicago') NOT IN (0,6)
        AND m.name IN ('BHM','BNA','DFW','SAT','HSV')
      GROUP BY 1, 2
    ),
    daily_addressable AS (
      SELECT
        m.name AS market,
        DATE(o.started_at AT TIME ZONE 'America/Chicago') AS day,
        COUNT(*) AS addressable
      FROM orders o
      JOIN projects p ON p.id = o.project_id
      JOIN markets m ON m.id = p.market_id
      WHERE o.deleted_at IS NULL
        AND o.started_at AT TIME ZONE 'America/Chicago' >= DATE_TRUNC('week', '{today_ct}'::date) - INTERVAL '21 days'
        AND DATE(o.started_at AT TIME ZONE 'America/Chicago') <= '{today_ct}'
        AND EXTRACT(DOW FROM o.started_at AT TIME ZONE 'America/Chicago') NOT IN (0,6)
        AND (o.status NOT IN ('canceled') OR o.driver_id IS NOT NULL)
        AND m.name IN ('BHM','BNA','DFW','SAT','HSV')
      GROUP BY 1, 2
    )
    SELECT
      dh.market,
      p.period_num,
      MIN(p.day) AS period_start,
      MAX(p.day) AS period_end,
      ROUND(AVG(COALESCE(dh.hauls, 0))::numeric, 1) AS avg_hauls,
      ROUND(AVG(COALESCE(dh.drivers, 0))::numeric, 1) AS avg_drivers,
      ROUND(AVG(CASE WHEN dh.drivers > 0 THEN dh.hauls::numeric / dh.drivers ELSE 0 END)::numeric, 1) AS avg_hauls_per_driver,
      ROUND(AVG(COALESCE(da.addressable, 0))::numeric, 1) AS avg_addressable
    FROM periods p
    CROSS JOIN (SELECT DISTINCT name FROM markets WHERE name IN ('BHM','BNA','DFW','SAT','HSV')) mk
    LEFT JOIN daily_hauls dh ON dh.day = p.day AND dh.market = mk.name
    LEFT JOIN daily_addressable da ON da.day = p.day AND da.market = mk.name
    GROUP BY dh.market, mk.name, p.period_num
    HAVING dh.market IS NOT NULL OR mk.name IS NOT NULL
    ORDER BY mk.name, p.period_num
    """

    try:
        daily_rows  = run_sql(daily_sql)
        today_rows  = run_sql(today_sql)
        mtd_rows    = run_sql(mtd_sql)
        period_rows = run_sql(period_sql)
    except Exception as e:
        print(f"DB query error: {e}")
        raise

    return {
        "today": str(today_ct),
        "daily_rows": daily_rows,
        "today_rows": today_rows,
        "mtd_rows": mtd_rows,
        "period_rows": period_rows,
        "targets": TARGETS,
        "norms": NORMS,
    }


# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a dashboard generator for Independent Waste (IW), a roll-off waste hauler.

You will receive pre-fetched data as JSON. Generate a complete, self-contained HTML dashboard.
Output ONLY raw HTML starting with <!DOCTYPE html> — no explanation, no markdown fences.

IW Brand colors: navy #1b1c51, orange #f26a21, green #00a775, bone background #f5f3ef
Font: system-ui, sans-serif

LAYOUT:
- Platform header at top: total productive hauls today, total addressable, current month MTD avg
- Markets stacked FULL WIDTH vertically, one per row, in order: DFW, BHM, SAT, BNA, HSV
- Each market section has two parts side by side: LEFT = chart (60% width), RIGHT = period breakdown (40% width)
- Action plan table below all markets
- Footer: "IW Ops Dashboard · {date} · Data via IW DB"

MARKET SECTION — LEFT SIDE (chart):
- Full width chart, height 160px
- Shows previous calendar month + current calendar month data only
- X-axis: business days only (Mon–Fri), no empty weekend gaps
- Market name, today's haul count, target range, verdict badge shown above chart
- Today hauls / Addressable / Active drivers shown as small stats row above chart

MARKET SECTION — RIGHT SIDE (period breakdown):
- 4 weekly periods: current week (Mon–today) + 3 prior full Mon–Sun weeks
- Label each: "Jun 2–8", "May 26–Jun 1", "May 19–25", "May 12–18" etc.
- For each period show:
  * Avg productive hauls/day (bold, large)
  * Verdict badge (same logic as below)
  * Stats row: X drivers · Y hauls/driver · Z avg addressable/day
- Stack periods vertically, most recent at top

VERDICT LOGIC — apply per period using that period's averages vs fixed norms:
1. Sales gap: avg_addressable_per_day < target_min  ← board was thin
2. Driver gap: avg_active_drivers <= p25_drivers from norms
3. Capacity ceiling: avg_hauls_per_driver >= p75_hauls_per from norms AND avg_hauls < target_min
4. Throughput gap: default — board full, drivers present, not at ceiling, still missing target
5. On target: avg_hauls >= target_min → green badge

VERDICT COLORS: Sales gap = orange, Driver gap = amber, Capacity ceiling = blue, Throughput gap = orange, On target = green

CHART REQUIREMENTS:
- Use Chart.js from https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js
- All chart initialization inside window.addEventListener('load', ...) — never inline
- Dataset order:
  [0] Target band top (target_max, fill '+1', backgroundColor 'rgba(0,167,117,0.13)', borderColor 'transparent', pointRadius 0)
  [1] Target band bottom (target_min, borderDash [3,3], borderColor 'rgba(0,167,117,0.35)', fill false, pointRadius 0)
  [2] Daily hauls line (borderColor '#d0ceca', borderWidth 1.5, fill false, pointRadius 0 except rain dots blue #4a90d9 pointRadius 4, spanGaps false)
  [3] 5-business-day rolling average (borderColor '#1b1c51', borderWidth 2.5, tension 0.4, fill false, pointRadius 0)
- Rolling avg: index i = mean of up to 5 values ending at i
- Rain dots: fetch Open-Meteo live in browser per market, mark days >= 2.54mm
  URL: https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lng}&daily=precipitation_sum&start_date={prev_month_start}&end_date={today}&timezone=America/Chicago
  Coords: BHM(33.5779,-86.7514), BNA(36.1245,-86.6782), DFW(32.8998,-97.0403), SAT(29.5337,-98.4698), HSV(34.6372,-86.7750)

ACTION PLAN TABLE:
- Only show markets where current week avg < target_min
- Rank by current week miss % (most off target first)
- Columns: Market | Current week avg | Verdict | Investigate note

Bake ALL haul data and period stats into the HTML as JS constants — computed server-side from the data provided.
Open-Meteo fetched live from browser only. No other live API calls from browser.

Output ONLY the HTML. Nothing else."""


def build_user_prompt(data: dict) -> str:
    return f"""Today is {data['today']} (Central Time).

Here is the pre-fetched data from the IW database:

DAILY HAULS (previous month + current month, weekends rolled into preceding Friday):
{json.dumps(data['daily_rows'], indent=2)}

TODAY'S SUMMARY PER MARKET:
{json.dumps(data['today_rows'], indent=2)}

MTD AVERAGE PER MARKET (current month):
{json.dumps(data['mtd_rows'], indent=2)}

WEEKLY PERIOD STATS (4 periods: period_num 0=current week, 1=last week, 2=two weeks ago, 3=three weeks ago):
{json.dumps(data['period_rows'], indent=2)}

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
