# TodoApp visual navigation bridge

This repository is a Vite React app. `vnav_bridge.py` runs it in a mobile Chromium window, sends clean screenshots to the separate visual-navigation server, moves a full-height vertical guide to the target x coordinate and then a full-width horizontal guide to y, and performs the returned tap or type action at their intersection. The server owns the planner and persistent context; this runner owns only the browser and HTTP exchange.

Create an ignored `.env.local` here with `VNAV_SERVER_URL=http://127.0.0.1:8765` and `VNAV_SERVER_TOKEN=<server token>`. Set `VNAV_GOAL` to override the default task-creation prompt. Do not commit this file.

Install dependencies once with `npm install`, `python3 -m venv .venv`, and `.venv/bin/pip install playwright`. On this machine, the system Chromium is used. Run from this directory with `.venv/bin/python vnav_bridge.py`. Set `VNAV_HEADLESS=1` for a background browser.

The runner uses protocol v1: `POST /v1/session`, then one `POST /v1/session/{id}/step` per screenshot with a stable frame ID and increasing sequence. It retries the same frame after a network timeout. The server returns coordinates in screenshot pixels; the runner scales them to the visible browser viewport before animation and execution. The screenshot overlay is removed before every capture. The server may take several minutes per frame on CPU.

For a one-action connection check, run `VNAV_HEADLESS=1 VNAV_MAX_STEPS=1 VNAV_SMOKE_TEST=1 .venv/bin/python vnav_bridge.py`. This starts a fresh session and executes one server action without claiming the full task is complete.
