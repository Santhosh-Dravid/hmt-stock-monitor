#!/usr/bin/env python3
"""
HMT Watches stock monitor (multi-site).

Watches one or more HMT storefronts and notifies you the moment any product
flips from out-of-stock to in-stock. Supported sites:

  - "smartbiz"       -> hmtwatches.store (Amazon SmartBiz JSON API)
  - "hmtwatches_in"  -> hmtwatches.in   (Laravel /filter_products endpoint)

Design:
  - Reliable: hits the same backend the website uses (no browser needed).
  - Stateful: remembers which items were in stock last run (per watcher), so
    you're only alerted on the TRANSITION out-of-stock -> in-stock (no spam).
  - Notifications: Telegram (instant phone push) and/or desktop notify-send.

Usage:
    python3 hmt_stock_monitor.py            # single check of all watchers
    python3 hmt_stock_monitor.py --once
    python3 hmt_stock_monitor.py --loop     # poll forever on an interval
    python3 hmt_stock_monitor.py --test-notify

Config: config.json next to this script (see config.example.json).
Env overrides: HMT_TG_TOKEN, HMT_TG_CHAT_ID.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "monitor.log"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SMARTBIZ_API = "https://smartpos.amazon.in/api-unauthenticated/resources/external/catalog/products"
SMARTBIZ_STORE = "https://www.hmtwatches.store"
HMTIN_BASE = "https://www.hmtwatches.in"

DEFAULT_CONFIG = {
    "poll_interval_seconds": 300,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "desktop_notification": True,
    "watchers": [
        {
            "name": "hmtwatches.store - For Him",
            "type": "smartbiz",
            "shop_id": 48236,
            "division": "d8fe9089-07c3-4912-82d3-05984e295547",
            "category_name": "For Him",
            "page_size": 100,
            "enabled": True,
        },
        {
            "name": "hmtwatches.in - Mens",
            "type": "hmtwatches_in",
            "gender_type": 1,
            "load_more_count": 500,
            "enabled": True,
        },
    ],
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(user)
        except (OSError, json.JSONDecodeError) as e:
            log(f"WARNING: could not read config.json ({e}); using defaults")
    if os.environ.get("HMT_TG_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["HMT_TG_TOKEN"]
    if os.environ.get("HMT_TG_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["HMT_TG_CHAT_ID"]
    if os.environ.get("HMT_DESKTOP_NOTIFICATION"):
        cfg["desktop_notification"] = (
            os.environ["HMT_DESKTOP_NOTIFICATION"].strip().lower()
            in ("1", "true", "yes", "on")
        )
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"WARNING: could not write state.json ({e})")


# ============================================================
# Watcher implementations
# Each returns a list of product dicts: {key, name, price, url, in_stock}
#   key  -> STABLE identity for the product (used for state tracking)
# ============================================================

def _headers(referer: str, json_body: bool = False) -> dict:
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html, */*",
        "Origin": referer.rstrip("/"),
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def fetch_smartbiz(w: dict) -> list[dict]:
    """hmtwatches.store — paginated JSON catalog API."""
    size = int(w.get("page_size", 100))
    out: list[dict] = []
    offset = 0
    while True:
        body = {
            "filter": {"division": w["division"], "isBestSeller": None, "skus": None},
            "limit": size,
            "offset": offset,
            "shopId": w["shop_id"],
        }
        r = requests.post(SMARTBIZ_API, headers=_headers(SMARTBIZ_STORE + "/", True),
                          json=body, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            if p.get("deactivated"):
                in_stock = False
            else:
                avail = ((p.get("buyingOptions") or {}).get("singlePurchase") or {}).get("availability") or {}
                cs = p.get("currentStock")
                in_stock = bool(avail.get("inStock") or avail.get("isBuyable")
                                or (isinstance(cs, (int, float)) and cs > 0))
            sku = p.get("sku")
            pid = p.get("primaryProductId") or sku
            name = p.get("name") or "Unknown"
            out.append({
                "key": f"smartbiz:{sku}",
                "name": name,
                "price": p.get("sellingPrice"),
                "url": f"{SMARTBIZ_STORE}/product/{pid}/{name.replace(' ', '%20')}",
                "in_stock": in_stock,
            })
        if len(batch) < size:
            break
        offset += size
        if offset > 5000:
            break
    return out


def fetch_hmtwatches_in(w: dict) -> list[dict]:
    """hmtwatches.in — Laravel POST /filter_products returns {status, html}.

    Stock signal: an out-of-stock card contains a <div class="outofstock">
    ("Coming Soon"). The per-request `id` token is randomised, so identity is
    keyed on the STABLE product image id (all_images/<id>.jpg).
    """
    data = {
        "load_more_count": int(w.get("load_more_count", 500)),
        "menu_val": w.get("menu_val", ""),
        "gender_type": int(w.get("gender_type", 1)),
    }
    r = requests.post(f"{HMTIN_BASE}/filter_products",
                      headers=_headers(f"{HMTIN_BASE}/mens"), data=data, timeout=30)
    r.raise_for_status()
    payload = r.json()
    frag = payload.get("html", "") if isinstance(payload, dict) else ""
    if not frag:
        return []

    cards = [c for c in re.split(r'(?=<div class="bc_p_item")', frag) if "bc_p_item" in c]
    out: list[dict] = []
    for idx, c in enumerate(cards):
        mname = re.search(r'class="bc_p_name"[^>]*>\s*<span>\s*(.*?)\s*</span>', c, re.DOTALL)
        name = re.sub(r"\s+", " ", mname.group(1)).strip() if mname else "Unknown"
        mprice = re.search(r"RS\.?\s*([0-9,]+)", c, re.I)
        price = int(mprice.group(1).replace(",", "")) if mprice else None
        mimg = re.search(r"all_images/([0-9]+)\.(?:jpg|jpeg|png|webp)", c, re.I)
        # token id is per-request-random, so DO NOT use it for identity
        img_id = mimg.group(1) if mimg else None
        # stable key: prefer image id; fall back to name+price+position
        key = f"hmtin:{img_id}" if img_id else f"hmtin:{name}|{price}|{idx}"
        # in stock = NO out-of-stock/coming-soon marker on the card
        oos = ("outofstock" in c.lower()) or ("coming soon" in c.lower())
        out.append({
            "key": key,
            "name": name,
            "price": price,
            "url": f"{HMTIN_BASE}/mens",
            "in_stock": not oos,
        })
    return out


FETCHERS = {
    "smartbiz": fetch_smartbiz,
    "hmtwatches_in": fetch_hmtwatches_in,
}


# ---------------- Notifications ----------------

def notify_telegram(cfg: dict, text_html: str) -> bool:
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text_html, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=20,
        )
        if r.status_code == 200:
            return True
        log(f"Telegram error {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"Telegram request failed: {e}")
    return False


def notify_desktop(cfg: dict, title: str, body: str) -> bool:
    if not cfg.get("desktop_notification"):
        return False
    try:
        import subprocess
        subprocess.run(["notify-send", "-u", "critical", "-a", "HMT Stock Monitor",
                        title, body], check=False)
        return True
    except (FileNotFoundError, OSError):
        return False


def send_restock_alert(cfg: dict, watcher_name: str, newly: list[dict]) -> None:
    lines = [f"<b>🟢 HMT restock — {html.escape(watcher_name)}</b>", ""]
    plain = []
    for p in newly:
        name = p.get("name", "Unknown")
        price = p.get("price")
        price_s = f"₹{int(price)}" if isinstance(price, (int, float)) else ""
        url = p.get("url", "")
        lines.append(f'• <a href="{html.escape(url)}">{html.escape(name)}</a> {price_s}')
        plain.append(f"{name} {price_s}")
    sent_tg = notify_telegram(cfg, "\n".join(lines))
    sent_dt = notify_desktop(cfg, f"HMT restock — {watcher_name} ({len(newly)})",
                             "\n".join(plain)[:400])
    log(f"ALERT [{watcher_name}]: {len(newly)} back in stock -> telegram={sent_tg} desktop={sent_dt}")
    if not sent_tg and not sent_dt:
        log("WARNING: no notification channel delivered. Configure Telegram or enable desktop.")


# ---------------- Per-watcher check ----------------

def check_watcher(cfg: dict, w: dict, state: dict) -> None:
    name = w.get("name", w.get("type", "watcher"))
    fetcher = FETCHERS.get(w.get("type"))
    if fetcher is None:
        log(f"[{name}] SKIP: unknown watcher type '{w.get('type')}'")
        return

    try:
        products = fetcher(w)
    except requests.RequestException as e:
        log(f"[{name}] network error: {e}")
        return

    if not products:
        log(f"[{name}] no products returned (empty/blocked). Skipping this run.")
        return

    wstate = state.setdefault(name, {})
    prev = set(wstate.get("in_stock_keys", []))

    in_stock = [p for p in products if p.get("in_stock")]
    cur = {p["key"] for p in in_stock}
    newly = [p for p in in_stock if p["key"] not in prev]

    log(f"[{name}] checked {len(products)} | in stock: {len(in_stock)} | newly: {len(newly)}")
    if newly:
        for p in newly:
            log(f"    + RESTOCKED: {p['name']} ({p.get('price')})")
        send_restock_alert(cfg, name, newly)

    wstate["in_stock_keys"] = sorted(cur)
    wstate["last_check"] = datetime.now().isoformat()
    wstate["last_in_stock_count"] = len(in_stock)


def check_all(cfg: dict) -> None:
    state = load_state()
    watchers = [w for w in cfg.get("watchers", []) if w.get("enabled", True)]
    if not watchers:
        log("No enabled watchers in config.")
        return
    for w in watchers:
        try:
            check_watcher(cfg, w, state)
        except Exception as e:  # never let one watcher kill the rest
            log(f"[{w.get('name', w.get('type'))}] unexpected error: {e}")
    save_state(state)


def main() -> int:
    ap = argparse.ArgumentParser(description="HMT watches stock monitor (multi-site)")
    ap.add_argument("--loop", action="store_true", help="run forever, polling on an interval")
    ap.add_argument("--once", action="store_true", help="single check (default)")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.test_notify:
        ok_tg = notify_telegram(cfg, "<b>✅ HMT Stock Monitor</b>\nTest notification — Telegram is working.")
        ok_dt = notify_desktop(cfg, "HMT Stock Monitor", "Test notification — desktop is working.")
        log(f"Test notify: telegram={ok_tg} desktop={ok_dt}")
        return 0 if (ok_tg or ok_dt) else 1

    if args.loop:
        interval = int(cfg.get("poll_interval_seconds", 300))
        log(f"Starting loop; polling every {interval}s. Ctrl-C to stop.")
        while True:
            try:
                check_all(cfg)
            except Exception as e:
                log(f"Unexpected error: {e}")
            time.sleep(interval)
    else:
        check_all(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
