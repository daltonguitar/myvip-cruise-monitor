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
TARGET_PARTNERS = ["norwegian", "royal caribbean", "virgin voyages", "ncl", "virgin", "cruise"]

def send_alert(message: str):
    print(f"\n[ALERT] {message}\n")
    if DISCORD_WEBHOOK_URL:
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
            if resp.status_code >= 400:
                print(f"Discord returned error {resp.status_code}: {resp.text}")
            else:
                print("Discord notification delivered successfully.")
        except Exception as e:
            print(f"Failed to send Discord alert: {e}")
    else:
        print("Warning: DISCORD_WEBHOOK_URL environment variable is empty or not set.")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Failed to send Telegram alert: {e}")

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
    print("Navigating to https://myvip.co/rewardstore ...")
    page.goto("https://myvip.co/rewardstore", wait_until="networkidle", timeout=60000)
    
    # Wait for React/client-side catalog rendering and scroll down to load lazy cards
    time.sleep(6)
    page.evaluate("window.scrollTo(0, 1000);")
    time.sleep(2)
    page.evaluate("window.scrollTo(0, 0);")

    # Scrape all clickable links and card-like containers
    items_data = page.evaluate("""() => {
        const results = [];
        // Catch all links and container cards
        const elements = document.querySelectorAll('a, div[role="button"], div[class*="card"], div[class*="Card"]');
        elements.forEach(el => {
            const text = (el.innerText || "").trim();
            const href = el.getAttribute('href') || (el.querySelector('a') ? el.querySelector('a').getAttribute('href') : '');
            if (text && text.length > 5 && text.length < 500) {
                results.push({ text: text, href: href || '' });
            }
        });
        return results;
    }""")

    print(f"Found {len(items_data)} raw page elements. Filtering for cruise partners...")

    matched = {}
    for item in items_data:
        text_lower = item["text"].lower()
        matched_partner = next((p for p in TARGET_PARTNERS if p in text_lower), None)
        if matched_partner:
            lines = [l.strip() for l in item["text"].split("\n") if l.strip()]
            title = lines[0] if lines else "Cruise Reward"
            is_sold_out = any(phrase in text_lower for phrase in ["sold out", "out of stock", "unavailable", "0 left"])
            
            # Create a stable identifier
            reward_id = item["href"] if item["href"] else f"{matched_partner}_{title}"
            
            matched[reward_id] = {
                "title": title,
                "partner": matched_partner.upper(),
                "available": not is_sold_out,
                "summary": " | ".join(lines[:3]),
                "link": f"https://myvip.co{item['href']}" if item['href'].startswith("/") else (item['href'] or "https://myvip.co/rewardstore")
            }

    return matched

def main():
    cached = load_cache()
    
    # Send a quick connection test on the very first run if cache is empty
    if not cached:
        send_alert("🚀 **myVIP Cruise Monitor Connected!** Setup confirmed and listening for restocks.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()
        current = check_rewards(page)

    print(f"Matched {len(current)} cruise-related entries.")
    for cid, data in current.items():
        print(f" - [{data['partner']}] {data['title']} (Available: {data['available']})")

    alerts_sent = 0
    for r_id, info in current.items():
        if r_id not in cached:
            status = "✅ AVAILABLE" if info["available"] else "❌ CURRENTLY SOLD OUT"
            send_alert(
                f"🚢 **NEW CRUISE REWARD DETECTED!**\n"
                f"**Partner:** {info['partner']}\n"
                f"**Reward:** {info['title']}\n"
                f"**Status:** {status}\n"
                f"**Link:** {info['link']}"
            )
            alerts_sent += 1
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(
                f"🚨 **CRUISE REWARD RESTOCKED!**\n"
                f"**Partner:** {info['partner']}\n"
                f"**Reward:** {info['title']}\n"
                f"**Status:** ✅ AVAILABLE NOW!\n"
                f"**Link:** {info['link']}"
            )
            alerts_sent += 1

    save_cache(current)
    print(f"Done. Processed {len(current)} rewards. Sent {alerts_sent} alerts.")

if __name__ == "__main__":
    main()
