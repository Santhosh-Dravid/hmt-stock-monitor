# HMT Watches Stock Monitor (multi-site)

Notifies you the moment a watch comes back in stock on **two** HMT storefronts:

| Site | What's watched | How it reads stock |
|------|----------------|--------------------|
| `hmtwatches.store` | category **For Him** | SmartBiz JSON API (`smartpos.amazon.in/...`), `buyingOptions.inStock` |
| `hmtwatches.in` | **/mens** listing | Laravel `POST /filter_products`; a card showing "Coming Soon" = out of stock |

Both are polled in a single run (no browser needed). It remembers which items
were in stock last run — **per site** — and only alerts on the transition
**out-of-stock → in-stock** (no repeat spam).

Each site is a "watcher" in `config.json`, so you can enable/disable or add more
categories independently.

Files:
- `hmt_stock_monitor.py` — the monitor
- `config.json` — your settings (watchers, poll interval, notification channels)
- `state.json` — auto-created; remembers last in-stock items per watcher
- `monitor.log` — append-only run log
- `hmt-stock-monitor.service` / `.timer` — systemd user units for scheduling

---

## 1. Quick test

```bash
cd /home/santhosh/santhosh/poc/hmt-stock-monitor
python3 hmt_stock_monitor.py --once
```

You'll see one line per site, e.g.:
```
[hmtwatches.store - For Him] checked 191 | in stock: 0 | newly: 0
[hmtwatches.in - Mens] checked 74 | in stock: 0 | newly: 0
```

---

## 2. Choose how you get notified

### Option A — Telegram (recommended: instant push to your phone)

1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts →
   copy the **bot token** (looks like `123456:ABC-DEF...`).
2. Send any message (e.g. "hi") to your new bot so it can message you back.
3. Get your **chat id**: open in a browser
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   and copy the number in `"chat":{"id":<THIS>}`.
4. Put both in `config.json`:
   ```json
   "telegram_bot_token": "123456:ABC-DEF...",
   "telegram_chat_id": "987654321"
   ```
5. Verify:
   ```bash
   python3 hmt_stock_monitor.py --test-notify
   ```
   You should get a Telegram message. (You can also set them via env vars
   `HMT_TG_TOKEN` / `HMT_TG_CHAT_ID` instead of editing the file.)

### Option B — Desktop popup (Linux, already ON)

`desktop_notification: true` in `config.json` uses `notify-send`. This only
fires when you're logged into the desktop session. Good as a backup; use
Telegram if you want alerts on your phone when away from the PC.

---

## 3. Run it on a schedule

### systemd user timer (recommended — survives logout with lingering)

```bash
mkdir -p ~/.config/systemd/user
cp hmt-stock-monitor.service hmt-stock-monitor.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hmt-stock-monitor.timer

# check it
systemctl --user list-timers | grep hmt
journalctl --user -u hmt-stock-monitor.service -n 20 --no-pager
```

To keep it running even when you're logged out:
```bash
sudo loginctl enable-linger santhosh
```

Change frequency by editing `OnUnitActiveSec=5min` in the `.timer`, then
`systemctl --user daemon-reload && systemctl --user restart hmt-stock-monitor.timer`.

> Note: desktop popups need the graphical session; if you run via linger with
> no session, prefer Telegram.

### Or cron (every 5 minutes)

```bash
crontab -e
```
add:
```
*/5 * * * * /usr/bin/python3 /home/santhosh/santhosh/poc/hmt-stock-monitor/hmt_stock_monitor.py --once >> /home/santhosh/santhosh/poc/hmt-stock-monitor/cron.log 2>&1
```

### Or a simple foreground loop (for testing)

```bash
python3 hmt_stock_monitor.py --loop
```

---

## 4. Watch more categories / sites

Add entries to the `watchers` array in `config.json`.

- **hmtwatches.store** category: copy the `smartbiz` block and set `division`
  to the category UUID from the URL (`/category/<UUID>/<name>`) and update
  `category_name`. `shop_id` (`48236`) stays the same.
- **hmtwatches.in** gender: copy the `hmtwatches_in` block. `gender_type: 1` is
  men; try `2` for women (add a watcher and run once to confirm the count).

Set `"enabled": false` on a watcher to pause it without deleting it.

---

## Notes / reliability

- Polling every 5 min is a good balance. You can go to 2–3 min; avoid going
  below ~60s to be polite to the servers.
- **hmtwatches.store** stock signal: `buyingOptions.singlePurchase.availability.inStock`
  / `isBuyable` (with `currentStock > 0` fallback).
- **hmtwatches.in** stock signal: a product card is out of stock when it shows a
  `outofstock` / "Coming Soon" block; identity is keyed on the product image id
  (the page's `id=` URL token is randomised per request, so it is NOT used).
- If a site ever starts blocking the plain request, the fallback is to drive the
  real site with headless Chrome (selenium is already in the `pib-scraper`
  venv) — ask and I'll add that mode for that watcher.
