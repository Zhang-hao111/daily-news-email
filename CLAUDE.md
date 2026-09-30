# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python script that automates daily and weekly news briefings: fetches RSS feeds from 26 sources across three pipelines (14 tech + 8 finance + 4 politics), uses DeepSeek AI to select the most newsworthy articles (~12 tech + ~4 politics + ~4 finance = 20 kept), analyzes each, generates a Markdown report with an AI daily overview, saves it to an Obsidian vault, links it from the daily note, and pushes via email and optional group-bot webhook. Every Monday it also auto-generates a weekly report summarizing the week's dailies.

## Commands

```bash
# Run the script directly
python send_email.py

# Dry run: full pipeline incl. real AI calls, but no Obsidian/email/webhook/state writes
# (report is written to logs/dry_run_YYYY-MM-DD.md)
python send_email.py --dry-run

# Generate the weekly report manually from last week's accumulated data
python send_email.py --weekly

# Install dependencies
pip install -r requirements.txt

# Set up Windows scheduled task (runs daily at 09:05)
setup_task.bat   # must run as administrator
```

## Architecture

Single-file script (`send_email.py`). Pipeline definitions, prompts and all RSS feeds live in **`config.yaml`** (validated at startup; adding a pipeline or feed is a config-only change). Three independent pipelines (tech / politics / finance, driven by the `PIPELINE` dict loaded from it) run concurrently and share the same stage functions:

1. **Per-pipeline run** (`run_pipeline(kind)`) — for each of `tech` (14 sources, ~12 articles kept), `politics` (4 sources: BBC World/Guardian/Al Jazeera/NYT World, ~4 kept) and `finance` (8 sources incl. 华尔街见闻, ~4 kept):
   - **RSS fetch** (`fetch_all_news`) — feeds fetched concurrently with `ThreadPoolExecutor`, supports RSS 2.0 and Atom (parses pubDate/updated/dc:date into `published_dt`), caps at 15 items per source; failures logged and skipped.
   - **Filter chain** — `dedupe_articles` (exact title/URL dupes) → `filter_stale` (drops articles older than `NEWS_MAX_AGE_HOURS`, TrendRadar-inspired; no-date items kept) → `filter_seen` (drops URLs pushed within the last 7 days, state in `state/seen_urls.json`, gitignored) → `filter_excluded` (digest posts + configurable `EXCLUDE_KEYWORDS` for soft ads); `FOCUS_KEYWORDS` hits are sorted to the front before AI selection.
   - **AI selection** (`ai_select`) — DeepSeek picks the most newsworthy articles with diversity (focus keywords passed as a priority hint); falls back to `balanced_sample` on failure or when `DEEPSEEK_API_KEY` is unset.
   - **Full-text fetch** (`fetch_full_text`) — concurrently fetches the article body of the ~20 selected items and extracts it with trafilatura into `full_text` (up to 3000 chars); short/failed extractions (anti-bot shells) are retried via the free `r.jina.ai` reader proxy before falling back to the RSS description; skipped if trafilatura is not installed.
   - **Source health** (`update_source_health` / `get_disabled_sources`) — per-run per-source counts recorded to `state/source_health.json` (cache-persisted); sources failing for ≥ `SOURCE_FAIL_DISABLE_DAYS` (default 7) consecutive days are skipped from fetching and flagged in the report header, re-probed every `SOURCE_PROBE_INTERVAL_DAYS` (default 14) days and auto-recovered on first success.
   - **AI analysis** (`analyze_all` / `analyze_article`) — articles analyzed concurrently (6 workers) with `full_text` (or RSS summary) in the prompt; outputs category/summary/key_data/comment plus a 1-10 importance `score` (`_parse_score`, 0 on failure); each call retried with backoff; per-article failure falls back to `fallback_result`. Results keep input order.
2. **Dedupe + overview + market + report + Obsidian** — `dedupe_events` merges cross-category same-event reports into the most informative one (one extra AI call returning keep/drop groups; skipped on failure/no key); `fetch_market_snapshot` pulls A股三大指数 from Eastmoney's public API into a `## 📈 市场快照` section (skipped on failure); `ai_overview` generates a 3-5 sentence daily overview; `generate_markdown` opens with a stats header (fetched → selected, per-category counts), then groups by category with items sorted by score; `save_to_obsidian` / `link_to_daily_note` skipped with a warning if `OBSIDIAN_VAULT_PATH` is unset.
3. **Push** (`send_email`, `push_webhook`) — email via QQ Mail SMTP_SSL with plain-text fallback part, plus optional group-bot webhook (飞书/钉钉/企业微信 via `PUSH_WEBHOOK_URL`/`PUSH_WEBHOOK_TYPE`); pushed URLs are then recorded to `state/seen_urls.json` so consecutive runs don't repeat articles. If email raises, the process exits 1 and seen-URLs are NOT recorded (next run retries those articles).

## TrendRadar-inspired Configuration (optional, all in `.env`)

- `NEWS_MAX_AGE_HOURS` (default 36) — freshness filter, old articles are dropped
- `FOCUS_KEYWORDS` / `EXCLUDE_KEYWORDS` — comma-separated; focus keywords boost articles in AI selection, exclude keywords drop soft-ad titles before AI
- `PUSH_WEBHOOK_URL` + `PUSH_WEBHOOK_TYPE` (`feishu`/`dingtalk`/`wecom`) — optional group-bot push in addition to email
- `.github/workflows/daily.yml` — optional serverless scheduling on GitHub Actions (cron 23:23 UTC = 约 07:23 北京时间); configure repo Secrets instead of `.env`; Obsidian steps auto-skip; use this OR the local `setup_task.bat`, not both (duplicate emails)
- `.github/workflows/keepalive.yml` — monthly empty commit so the 60-day-inactivity rule never disables the scheduled workflow

## Weekly Report

- Each daily run appends its selected articles (title/date/source/url/category/summary/key_data/comment) to `state/week_<ISO周>.json` (`record_week_items`), deduped by URL.
- On Monday runs (`weekday() == 0`), `generate_weekly_report` loads **last week's** file, AI picks the ~15 most important items of the week (`ai_select_weekly`, falls back to `balanced_weekly_sample`), generates a weekly overview, and saves `weekly-<label>.md` to Obsidian + emails it as `每周热点汇报 - <label>`. Weekly failure never breaks the daily report.
- `python send_email.py --weekly` triggers the weekly report manually (uses last week's accumulated data).
- Categories: tech 前沿科技/互联网产业/消费电子/科技创投/国际科技, politics 国际政治, finance 美股市场; 华尔街见闻 partially fills the 国内财经 gap (A股专源仍缺).

## Logging & Errors

- Logging goes to console and `logs/YYYYMM.log` (gitignored) — check the log file when the scheduled task produces no email.
- Any uncaught exception is logged with traceback and the process exits with code 1, so a daily run never fails silently.

## Configuration

All secrets and paths are in `.env` (loaded via `python-dotenv`). See `.env.example` for required keys:

- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` — DeepSeek API credentials
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_AUTH_CODE` / `EMAIL_TO` — QQ Mail SMTP config
- `OBSIDIAN_VAULT_PATH` — path to Obsidian vault

## Key Details

- AI model: `deepseek-chat` via OpenAI SDK (base_url override), configurable via `DEEPSEEK_MODEL`
- Three independent pipelines: tech (14 sources, ~12 kept), politics (4 sources, ~4 kept), finance (8 sources, ~4 kept) — total 20 articles/day, run concurrently and merged before report generation
- AI selection: `ai_select` picks best articles with diversity for both pipelines (prompts differ via `PIPELINE`); falls back to `balanced_sample` on failure
- Category order is hardcoded in `generate_markdown` — first 3 items per category are "important" (full detail), rest are brief links
- Tech categories: 前沿科技, 互联网产业, 消费电子, 科技创投, 国际科技
- Finance categories: 美股市场
- A股专源仍缺（国内财经站无标准RSS，RSSHub国内不可用）；华尔街见闻可部分覆盖国内财经动态
- Obsidian daily note format: `YYYY.MM.DD.md` with `# 📅` heading; auto-creates if missing
- All AI calls go through `call_with_retry` (exponential backoff); JSON responses are parsed by `parse_json` which strips markdown code fences
