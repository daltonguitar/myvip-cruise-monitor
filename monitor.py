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

PARTNER_TARGETS = [
    {"name": "Norwegian Cruise Line", "url": "https://myvip.co/rewardstore/partner/66"},
    {"name": "Royal Caribbean", "url": "https://myvip.co/rewardstore/partner/29"},
]

def send_alert(title: str, partner: str, status: str, link: str, points: str = ""):
    print(f"\n[ALERT] {partner} | {title} | {status}\n")

    if DISCORD_WEBHOOK_URL:
        embed_color = 5763719 if "AVAILABLE" in status and "SOLD OUT" not in status else 15548997
        embed = {
            "title": f"🚢 {title}",
            "url": link,
            "color": embed_color,
            "fields": [
                {"name": "Partner", "value": partner, "inline": True},
                {"name": "Status", "value": status, "inline": True},
            ]
        }
        if points:
            embed["fields"].append({"name": "Loyalty Points", "value": points, "inline": True})

        payload = {
            "content": f"🚨 **Cruise Alert:** {title}",
            "embeds": [embed]
        }
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            if resp.status_code >= 400:
                print(f"Discord error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"Failed to post to Discord: {e}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_text = f"🚢 *{title}*\n*Partner:* {partner}\n*Status:* {status}\n*Points:* {points or 'N/A'}\n[View Reward]({link})"
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": tg_text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")

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

def expand_all_rewards(page):
    """Clicks 'See More' until all cards on the partner grid are displayed."""
    for i in range(8):
        try:
            btn = page.locator("button:has-text('See More'), button:has-text('Load More'), a:has-text('See More')").first
            if btn.is_visible(timeout=2000):
                print(f"Expanding rewards list (click {i+1})...")
                btn.click()
                time.sleep(2)
            else:
                break
        except Exception:
            break

def check_reward_availability(page, card_locator, partner_name: str) -> dict:
    """Clicks into the card, checks the actual Redeem button state, then closes it."""
    try:
        raw_text = card_locator.inner_text().strip()
        lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
        if len(lines) < 2:
            return None

        title = lines[0]
        if title.lower() in ["norwegian", "royal caribbean", "virgin voyages"] and len(lines) > 1:
            title = lines[1]

        points = ""
        for line in lines:
            if any(c.isdigit() for c in line) and any(k in line.lower() for k in ["pts", "points", "lp", ","]):
                points = line
                break

        # Click the card to open its detail drawer/modal
        card_locator.click()
        time.sleep(2)

        # Inspect the modal's primary action button
        is_available = page.evaluate("""() => {
            // Find all buttons inside dialogs, modals, or slideouts
            const buttons = Array.from(document.querySelectorAll('div[role="dialog"] button, [class*="modal"] button, [class*="drawer"] button, button[class*="Redeem"], button[class*="purchase"]'));
            
            for (const b of buttons) {
                const text = (b.innerText || '').toLowerCase().trim();
                if (!text) continue;

                if (text.includes('sold out') || text.includes('out of stock') || text.includes('unavailable')) {
                    return false;
                }
                if (text.includes('redeem') || text.includes('get reward') || text.includes('purchase')) {
                    const isDisabled = b.disabled || b.getAttribute('aria-disabled') === 'true' || b.classList.contains('disabled');
                    return !isDisabled;
                }
            }
            
            // Check if full page/modal body text mentions sold out
            const body = document.body.innerText.toLowerCase();
            if (body.includes('sold out') || body.includes('out of stock') || body.includes('all rewards claimed')) {
                return false;
            }

            return false; // Default safe: if we can't confirm a live Redeem button, it's NOT available
        }""")

        # Close the modal (press Escape or click the close button)
        try:
            close_btn = page.locator("button[aria-label*='close' i], button:has-text('✕'), [class*='Close']").first
            if close_btn.is_visible(timeout=1000):
                close_btn.click()
            else:
                page.keyboard.press("Escape")
        except Exception:
            page.keyboard.press("Escape")

        time.sleep(1)

        return {
            "title": title,
            "partner": partner_name,
            "available": is_available,
            "points": points,
            "link": page.url
        }
    except Exception as e:
        print(f"Error checking card: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None

def scrape_partner(page, partner_info: dict) -> dict:
    url = partner_info["url"]
    partner_name = partner_info["name"]
    print(f"\nNavigating to {partner_name} ({url}) ...")

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(4)

    # Scroll down to load initial cards
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(1.5)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.5)

    expand_all_rewards(page)
    time.sleep(2)

    # Find all card elements on the partner grid
    card_locators = page.locator("div[class*='Card'], div[class*='card'], a[href*='/reward/']").all()
    print(f"Found {len(card_locators)} card elements for {partner_name}.")

    partner_rewards = {}
    for idx, card in enumerate(card_locators):
        try:
            text = card.inner_text().strip()
            # Skip empty or irrelevant containers
            if len(text) < 15 or "all partners" in text.lower() or "see more" in text.lower():
                continue

            result = check_reward_availability(page, card, partner_name)
            if result and result["title"]:
                r_id = f"{partner_name}_{result['title']}"
                partner_rewards[r_id] = result
                status_str = "✅ IN STOCK" if result["available"] else "❌ SOLD OUT"
                print(f" -> [{partner_name}] {result['title']} | {status_str}")
        except Exception as e:
            continue

    return partner_rewards

def main():
    cached = load_cache()
    current_all = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()

        for partner in PARTNER_TARGETS:
            try:
                rewards = scrape_partner(page, partner)
                current_all.update(rewards)
            except Exception as e:
                print(f"Error scanning {partner['name']}: {e}")

    print(f"\nScan complete. Total rewards evaluated: {len(current_all)}")

    alerts_sent = 0
    for r_id, info in current_all.items():
        # Case 1: Brand-new item dropped and it is ACTUALLY available
        if r_id not in cached:
            if info["available"]:
                send_alert(
                    title=info["title"],
                    partner=info["partner"],
                    status="✨ NEW DROP (✅ AVAILABLE NOW)",
                    link=info["link"],
                    points=info["points"]
                )
                alerts_sent += 1
            else:
                print(f"New item found in archive, but sold out: {info['title']} (no alert sent)")

        # Case 2: Restocked (was false -> now true)
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="🚨 RESTOCKED & AVAILABLE NOW!",
                link=info["link"],
                points=info["points"]
            )
            alerts_sent += 1

        # Case 3: Sold out (was true -> now false)
        elif cached[r_id]["available"] and not info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="⚠️ RECENTLY SOLD OUT",
                link=info["link"],
                points=info["points"]
            )
            alerts_sent += 1

    save_cache(current_all)
    print(f"Finished. Sent {alerts_sent} alerts.")

if __name__ == "__main__":
    main()
