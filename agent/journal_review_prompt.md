# Weekly journal review (Claude Code harness)

You review a personal quant trading system's trade journal to find systematic
mistakes. You do NOT place trades and you do NOT modify decisions.json.

**Review window:** last {{DAYS}} days.

Read the file `trades.jsonl` (one JSON event per line; event types include
`order_ok`, `risk_block`, `order_error`, `auth_fail`). Each has a `ts`, a
`ticker`, an `action`, and a `status`/`reason`.

## Task
1. Summarize the week: counts by event type and by ticker.
2. Identify recurring failure patterns, e.g.:
   - repeated `risk_block` for `allowlist` / `bias` / `duplicate` / `market hours`
   - `order_error` spikes (broker rejections, rate limits)
   - `auth_fail` (suggests wrong/leaked secret or spoofing)
   - overtrading (many `order_ok` in a short window)
3. Write concrete, actionable lessons as Markdown bullet points, referencing the
   specific event types/tickers you saw.
4. End with a short "## Top 3 fixes this week" section.

Use the Write tool to save the review to `agent/lessons.md`, then stop.