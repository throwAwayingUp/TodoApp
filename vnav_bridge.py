#!/usr/bin/env python3
"""Run this Vite app in a mobile browser against the protocol-v1 VNAV server."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time
from urllib import error, request
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
APP_URL = os.getenv("VNAV_APP_URL", "http://127.0.0.1:5173/")
DEFAULT_GOAL = "Create a task called set alarm tmrw for 10"
OVERLAY_ID = "vnav-bridge-overlay"
REQUEST_TIMEOUT = 600
MAX_STEPS = int(os.getenv("VNAV_MAX_STEPS", "30"))
if MAX_STEPS < 1:
    raise ValueError("VNAV_MAX_STEPS must be positive")


def config() -> tuple[str, str, str]:
    local = ROOT / ".env.local"
    if local.exists():
        for line in local.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"VNAV_SERVER_URL", "VNAV_SERVER_TOKEN", "VNAV_GOAL"}:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    base = os.getenv("VNAV_SERVER_URL", "").rstrip("/")
    token = os.getenv("VNAV_SERVER_TOKEN", "")
    if not base or not token:
        raise RuntimeError("Set VNAV_SERVER_URL and VNAV_SERVER_TOKEN in .env.local")
    return base, token, os.getenv("VNAV_GOAL", DEFAULT_GOAL)


def exchange(base: str, token: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        base + path, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"VNAV HTTP {exc.code}: {detail}") from exc
    if not isinstance(result, dict) or result.get("protocol_version") != 1:
        raise ValueError("VNAV returned an invalid protocol-v1 response")
    return result


def start_session(base: str, token: str, goal: str) -> dict:
    result = exchange(base, token, "/v1/session", {
        "protocol_version": 1, "client_id": "todoapp-playwright", "goal": goal,
    })
    if not all(isinstance(result.get(k), str) and result[k] for k in ("session_id", "run_id")):
        raise ValueError("Session response is missing an ID")
    if result.get("next_sequence") != 1:
        raise ValueError("Session did not start at sequence 1")
    return result


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("Browser did not return a PNG")
    return struct.unpack(">II", data[16:24])


def capture(page, sequence: int) -> dict:
    page.evaluate("id => document.getElementById(id)?.remove()", OVERLAY_ID)
    page.wait_for_timeout(350)
    png = page.screenshot(type="png", scale="device", animations="disabled")
    width, height = png_size(png)
    return {
        "protocol_version": 1, "sequence": sequence, "frame_id": str(uuid4()),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "image_format": "png", "image_base64": base64.b64encode(png).decode("ascii"),
        "pixel_width": width, "pixel_height": height,
    }


def validate_step(result: dict, session: dict, frame: dict) -> dict | None:
    for key in ("session_id", "run_id"):
        if result.get(key) != session[key]:
            raise ValueError(f"Step {key} differs from the active session")
    for key in ("sequence", "frame_id", "pixel_width", "pixel_height"):
        if result.get(key) != frame[key]:
            raise ValueError(f"Step {key} differs from the captured screenshot")
    if result.get("coordinate_space") != "screenshot_pixels":
        raise ValueError("Server did not return screenshot-pixel coordinates")
    if result.get("status") == "complete" and result.get("edge") is None:
        return None
    if result.get("status") != "action" or not isinstance(result.get("edge"), dict):
        raise ValueError("Server returned neither an action nor completion")
    edge = result["edge"]
    if edge.get("method") not in ("tap", "type") or not isinstance(edge.get("semantic_label"), str):
        raise ValueError("Unsupported or incomplete edge")
    point, box = edge.get("click_point_px"), edge.get("bbox_px")
    width, height = frame["pixel_width"], frame["pixel_height"]
    if (not isinstance(point, list) or len(point) != 2 or
            any(type(n) not in (int, float) for n in point) or
            not (0 <= point[0] < width and 0 <= point[1] < height)):
        raise ValueError("Edge click point lies outside screenshot")
    if (not isinstance(box, list) or len(box) != 4 or
            any(type(n) not in (int, float) for n in box) or
            not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
        raise ValueError("Edge bounding box lies outside screenshot")
    args = edge.get("arguments")
    if edge["method"] == "type" and (
        not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str)
    ):
        raise ValueError("Type edge requires one text argument")
    return edge


def mapped_target(frame: dict, edge: dict, rect: dict) -> tuple[list[float], list[float]]:
    sx = rect["width"] / frame["pixel_width"]
    sy = rect["height"] / frame["pixel_height"]
    x, y = edge["click_point_px"]
    x1, y1, x2, y2 = edge["bbox_px"]
    return ([rect["left"] + x * sx, rect["top"] + y * sy],
            [rect["left"] + x1 * sx, rect["top"] + y1 * sy,
             rect["left"] + x2 * sx, rect["top"] + y2 * sy])


OVERLAY = """async ({id, point, box, label, details}) => {
  document.getElementById(id)?.remove();
  const root = document.createElement('div'); root.id = id;
  root.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;color:white;font:13px system-ui';
  const vertical = document.createElement('div');
  vertical.style.cssText = 'position:absolute;top:0;bottom:0;left:50%;width:3px;transform:translateX(-50%);background:#36f7dc;box-shadow:0 0 0 1px #002437,0 0 16px 4px #36f7dcbd';
  const horizontal = document.createElement('div');
  horizontal.style.cssText = 'position:absolute;left:0;right:0;top:50%;height:3px;transform:translateY(-50%);background:#36f7dc;box-shadow:0 0 0 1px #002437,0 0 16px 4px #36f7dcbd';
  const intersection = document.createElement('div');
  intersection.style.cssText = 'position:absolute;width:20px;height:20px;border:3px solid #fff;border-radius:3px;transform:translate(-50%,-50%) rotate(45deg);box-shadow:0 0 0 2px #002437,0 0 18px 5px #36f7dc;opacity:0';
  intersection.style.left = point[0] + 'px'; intersection.style.top = point[1] + 'px';
  const outline = document.createElement('div');
  outline.style.cssText = 'position:absolute;border:3px solid #ffec54;background:#ffec5420;border-radius:6px;box-shadow:0 0 14px #ffec54;display:none';
  const panel = document.createElement('div');
  panel.style.cssText = 'position:absolute;left:8px;right:8px;background:#0a182de8;border:1px solid #36f7dc;border-radius:10px;padding:10px;white-space:pre-wrap;box-shadow:0 4px 18px #0009';
  panel.style[point[1] > window.innerHeight / 2 ? 'top' : 'bottom'] = '8px';
  panel.textContent = label + '\\n' + details;
  root.append(vertical, horizontal, outline, intersection, panel); document.body.append(root);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  vertical.style.transition = 'left 650ms ease-in-out'; vertical.style.left = point[0] + 'px';
  await new Promise(r => setTimeout(r, 750));
  horizontal.style.transition = 'top 650ms ease-in-out'; horizontal.style.top = point[1] + 'px';
  await new Promise(r => setTimeout(r, 750));
  outline.style.left = box[0] + 'px'; outline.style.top = box[1] + 'px';
  outline.style.width = (box[2] - box[0]) + 'px'; outline.style.height = (box[3] - box[1]) + 'px';
  outline.style.display = 'block';
  intersection.style.transition = 'opacity 180ms ease'; intersection.style.opacity = '1';
  await new Promise(r => setTimeout(r, 900));
}"""


def act(page, edge: dict, point: list[float]) -> None:
    if edge["method"] == "tap":
        page.mouse.click(*point)
    else:
        marker = uuid4().hex
        target = page.evaluate("""({x,y,marker}) => {
          const el = document.elementFromPoint(x,y)?.closest('input,textarea,[contenteditable="true"]');
          if (!el) return null;
          el.setAttribute('data-vnav-target', marker);
          return {tag: el.tagName.toLowerCase(), editable: el.isContentEditable};
        }""", {"x": point[0], "y": point[1], "marker": marker})
        if target is None:
            raise RuntimeError("Type target is not an editable field")
        try:
            if target["tag"] in ("input", "textarea"):
                page.locator(f'[data-vnav-target="{marker}"]').fill(edge["arguments"][0])
            elif target["editable"]:
                page.mouse.click(*point)
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.insert_text(edge["arguments"][0])
        finally:
            page.evaluate("""marker => document.querySelector('[data-vnav-target="' + marker + '"]')
              ?.removeAttribute('data-vnav-target')""", marker)
    page.wait_for_timeout(450)


def app_server() -> subprocess.Popen | None:
    try:
        with socket.create_connection(("127.0.0.1", 5173), timeout=1):
            return None
    except OSError:
        pass
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm is required to run TodoApp")
    process = subprocess.Popen(
        [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with request.urlopen(APP_URL, timeout=2) as response:
                if response.status == 200:
                    return process
        except (error.URLError, TimeoutError):
            time.sleep(0.5)
    process.terminate()
    raise RuntimeError("TodoApp did not start on port 5173")


def run() -> None:
    base, token, goal = config()
    health = exchange(base, token, "/v1/health")
    if health.get("status") != "ready":
        raise RuntimeError("VNAV server is not ready")
    app_process = app_server()
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=os.getenv("VNAV_HEADLESS") == "1",
                executable_path=shutil.which("chromium") or shutil.which("chromium-browser"),
            )
            try:
                context = browser.new_context(viewport={"width": 450, "height": 800},
                                              device_scale_factor=int(os.getenv("VNAV_SCALE", "1")),
                                              is_mobile=True, has_touch=True)
                page = context.new_page()
                page.goto(APP_URL, wait_until="commit", timeout=30000)
                page.get_by_role("button", name="Add").wait_for(state="visible", timeout=90000)
                session = start_session(base, token, goal)
                print(f"VNAV session {session['run_id']} goal: {goal}", flush=True)
                for sequence in range(1, MAX_STEPS + 1):
                    frame = capture(page, sequence)
                    path = f"/v1/session/{session['session_id']}/step"
                    while True:
                        try:
                            result = exchange(base, token, path, frame)
                            edge = validate_step(result, session, frame)
                            break
                        except (error.URLError, TimeoutError) as exc:
                            print(f"Step {sequence} timed out; retrying same frame: {exc}", flush=True)
                    if edge is None:
                        print(f"Goal complete: {result.get('run_diagnostics')}", flush=True)
                        return
                    rect = page.evaluate("() => ({left:0, top:0, width:document.documentElement.clientWidth, height:window.innerHeight})")
                    point, box = mapped_target(frame, edge, rect)
                    diag = result.get("step_diagnostics", {})
                    print(f"Step {sequence}: {edge['method']} {edge['semantic_label']} @ {edge['click_point_px']} | {diag}", flush=True)
                    page.evaluate(OVERLAY, {"id": OVERLAY_ID, "point": point, "box": box,
                                            "label": edge["semantic_label"],
                                            "details": f"{edge['method']} @ {edge['click_point_px']} | {diag.get('context_result')} | {diag.get('tokens', {}).get('all', {})}"})
                    page.evaluate("id => document.getElementById(id)?.remove()", OVERLAY_ID)
                    act(page, edge, point)
                if os.getenv("VNAV_SMOKE_TEST") == "1":
                    print(f"Smoke test completed {MAX_STEPS} screenshot/action steps", flush=True)
                    return
                raise RuntimeError(f"Goal did not complete within {MAX_STEPS} steps")
            finally:
                browser.close()
    finally:
        if app_process is not None:
            app_process.terminate()


if __name__ == "__main__":
    run()
