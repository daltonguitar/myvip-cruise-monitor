import json
import os
import time
from playwright.sync_api import sync_playwright
import requests

# ----------------- CONFIGURATION -----------------
# Choose your alert method (Telegram or Discord)
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # From @BotFather
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"

DISCORD_WEBHOOK_URL = ""  # Optional: paste your Discord Webhook URL here

CACHE_FILE = "rewards_cache.json"
CHECK_INTERVAL_SECONDS = 300  # Check every 5 minutes (adjust as needed)

TARGET_PARTNERS = [
    "norwegian",
    "royal caribbean",
    "virgin voyages",
    "ncl",
    "virgin"
]
# --------------------------------------------------

def send_alert(message: str):
    """Sends notification to Telegram and/or Discord."""
    print(f"\n[ALERT] {message}\n")
    
    # Send via Telegram
    if TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN" and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Failed to send Telegram alert: {e}")

    # Send via Discord
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
    """Navigates to the store and scrapes cruise partner reward cards."""
    page.goto("https://myvip.co/rewardstore", wait_until="networkidle", timeout=60000)
    
    # Wait an extra 3 seconds for dynamic catalog tiles to finish mounting
    time.sleep(3)

    # Evaluate JavaScript in page context to collect all reward card elements
    rewards = page.evaluate("""() => {
        const items = [];
        // Target common card containers used on myVIP
        const cards = document.querySelectorAll('div[class*="RewardCard"], div[class*="reward-card"], a[href*="/reward/"]');
        
        cards.forEach(card => {
            const text = card.innerText || "";
            const linkEl = card.tagName === 'A' ? card : card.querySelector('a');
            const href = linkEl ? linkEl.getAttribute('href') : '';
            
            // Check sold out / out of stock indicators
            const isSoldOut = /sold out|out of stock|unavailable/i.test(text);

            items.push({
                text: text,
                href: href,
                is_available: !isSoldOut
            });
        });
        return items;
    }""")

    matched_rewards = {}

    for item in rewards:
        item_text_lower = item["text"].lower()
        
        # Check if the card belongs to any target cruise partner
        matched_partner = next((p for p in TARGET_PARTNERS if p in item_text_lower), None)
        if matched_partner:
            # Extract basic title line from the card text
            lines = [l.strip() for l in item["text"].split("\n") if l.strip()]
            title = lines[0] if lines else "Cruise Reward"
            
            # Use link or title as the unique identifier key
            reward_id = item["href"] if item["href"] else f"{matched_partner}_{title}"
            
            matched_rewards[reward_id] = {
                "title": title,
                "partner": matched_partner.title(),
                "available": item["is_available"],
                "details": " | ".join(lines[:3]),
                "link": f"https://myvip.co{item['href']}" if item["href"].startswith("/") else item["href"]
            }

    return matched_rewards


def run_monitor():
    print("Starting myVIP Cruise Rewards Monitor...")
    cached_rewards = load_cache()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Using a standard desktop user agent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        while True:
            try:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking reward store...")
                current_rewards = check_rewards(page)

                for r_id, info in current_rewards.items():
                    # 1. New Reward found
                    if r_id not in cached_rewards:
                        status = "✅ AVAILABLE" if info["available"] else "❌ SOLD OUT"
                        msg = (
                            f"🚢 *NEW CRUISE REWARD DETECTED!*\n"
                            f"*Partner:* {info['partner']}\n"
                            f"*Title:* {info['title']}\n"
                            f"*Status:* {status}\n"
                            f"*Link:* {info['link'] or 'https://myvip.co/rewardstore'}"
                        )
                        send_alert(msg)

                    # 2. Existing reward was restocked
                    elif not cached_rewards[r_id]["available"] and info["available"]:
                        msg = (
                            f"🚨 *CRUISE REWARD RESTOCKED!*\n"
                            f"*Partner:* {info['partner']}\n"
                            f"*Title:* {info['title']}\n"
                            f"*Status:* ✅ AVAILABLE NOW!\n"
                            f"*Link:* {info['link'] or 'https://myvip.co/rewardstore'}"
                        )
                        send_alert(msg)

                # Update cache and save
                cached_rewards = current_rewards
                save_cache(cached_rewards)

            except Exception as e:
                print(f"Error during cycle: {e}")

            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_monitor()
