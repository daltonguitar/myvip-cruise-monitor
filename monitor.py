import os
import sys
import json
import time
from playwright.sync_api import sync_playwright
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

CACHE_FILE = "rewards_cache.json"
TARGET_PARTNERS = ["norwegian", "royal caribbean", "virgin voyages", "ncl", "virgin"]

def send_alert(message: str):
    print(f"\n[ALERT] {message}\n")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Failed to send Telegram alert: {e}")

    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        except Exception as e:
            print(f"Failed to send Discord alert: {e}")

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def check_rewards(page) -> dict:
    page.goto("https://myvip.co/rewardstore", wait_until="networkidle", timeout=60000)
    time.sleep(3)

    rewards = page.evaluate("""() => {
        const items = [];
        const cards = document.querySelectorAll('div[class*="RewardCard"], div[class*="reward-card"], a[href*="/reward/"]');
        cards.forEach(card => {
            const text = card.innerText || "";
            const linkEl = card.tagName === 'A' ? card : card.querySelector('a');
            const href = linkEl ? linkEl.getAttribute('href') : '';
            const isSoldOut = /sold out|out of stock|unavailable/i.test(text);
            items.push({ text: text, href: href, is_available: !isSoldOut });
        });
        return items;
    }""")

    matched = {}
    for item in rewards:
        text_lower = item["text"].lower()
        matched_partner = next((p for p in TARGET_PARTNERS if p in text_lower), None)
        if matched_partner:
            lines = [l.strip() for l in item["text"].split("\n") if l.strip()]
            title = lines[0] if lines else "Cruise Reward"
            reward_id = item["href"] if item["href"] else f"{matched_partner}_{title}"
            matched[reward_id] = {
                "title": title,
                "partner": matched_partner.title(),
                "available": item["is_available"],
                "link": f"https://myvip.co{item['href']}" if item["href"].startswith("/") else item["href"]
            }
    return matched

def main():
    cached = load_cache()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        current = check_rewards(page)

    alerts_sent = 0
    for r_id, info in current.items():
        if r_id not in cached:
            status = "✅ AVAILABLE" if info["available"] else "❌ SOLD OUT"
            send_alert(f"🚢 *NEW CRUISE REWARD DETECTED!*\n*Partner:* {info['partner']}\n*Title:* {info['title']}\n*Status:* {status}\n*Link:* {info['link']}")
            alerts_sent += 1
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(f"🚨 *CRUISE REWARD RESTOCKED!*\n*Partner:* {info['partner']}\n*Title:* {info['title']}\n*Status:* ✅ AVAILABLE NOW!\n*Link:* {info['link']}")
            alerts_sent += 1

    save_cache(current)
    print(f"Cycle completed. {len(current)} target rewards found. {alerts_sent} alerts triggered.")

if __name__ == "__main__":
    main()
