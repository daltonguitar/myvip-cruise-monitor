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

# Targets: Brand names, ship identifiers, or cruise lines
TARGET_CRUISE_KEYWORDS = [
    "norwegian", "royal caribbean", "virgin voyages", 
    "spectrum of the seas", "anthem of the seas", "voyager of the seas",
    "ovation of the seas", "ncl", "virgin", "scarlet lady", "valiant lady", "resilient lady"
]

# Words that indicate a generic brand header tile or category button rather than an actual redeemable reward
GENERIC_IGNORE = [
    "explore all", "view all", "terms & conditions", "how it works",
    "filter", "rewards store", "category", "pacific coast highway"
]

def send_alert(title: str, partner: str, status: str, link: str, points: str = "", details: str = ""):
    print(f"\n[ALERT] {partner} | {title} | {status}\n")

    if DISCORD_WEBHOOK_URL:
        embed_color = 5763719 if "AVAILABLE" in status else 15548997  # Green vs Red
        embed = {
            "title": f"🚢 {title}",
            "url": link,
            "color": embed_color,
            "fields": [
                {"name": "Cruise Line / Partner", "value": partner, "inline": True},
                {"name": "Status", "value": status, "inline": True},
            ]
        }
        if points:
            embed["fields"].append({"name": "Loyalty Points", "value": points, "inline": True})
        if details:
            embed["fields"].append({"name": "Details", "value": details[:300], "inline": False})

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
        tg_text = (
            f"🚢 *{title}*\n"
            f"*Partner:* {partner}\n"
            f"*Status:* {status}\n"
            f"*Points:* {points or 'N/A'}\n"
            f"[View Reward]({link})"
        )
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

def check_rewards(page) -> dict:
    print("Navigating to https://myvip.co/rewardstore ...")
    page.goto("https://myvip.co/rewardstore", wait_until="networkidle", timeout=60000)
    time.sleep(5)

    # Attempt to click Travel / Cruises filter category if visible to filter noise
    try:
        cruise_tab = page.locator("text=/Travel|Cruise/i").first
        if cruise_tab.is_visible():
            cruise_tab.click()
            time.sleep(3)
    except Exception:
        pass

    # Scroll down multiple times to trigger lazy-loaded catalog tiles
    for scroll in [1000, 2000, 3000, 0]:
        page.evaluate(f"window.scrollTo(0, {scroll});")
        time.sleep(1.5)

    # Extract all distinct cards with their full text breakdown
    cards_data = page.evaluate("""() => {
        const results = [];
        const cards = document.querySelectorAll('div[class*="Card"], div[class*="card"], a[href*="/reward/"], div[role="button"]');
        
        cards.forEach(card => {
            const rawText = card.innerText || "";
            const lines = rawText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            
            // Skip elements that are just containers of other cards or too short
            if (lines.length < 2 || rawText.length > 600) return;

            const linkEl = card.tagName === 'A' ? card : card.querySelector('a');
            const href = linkEl ? linkEl.getAttribute('href') : '';

            // Check stock status
            const soldOut = /sold out|out of stock|unavailable|0 remaining/i.test(rawText);

            results.push({
                rawText: rawText,
                lines: lines,
                href: href || '',
                is_available: !soldOut
            });
        });
        return results;
    }""")

    print(f"Scanned {len(cards_data)} potential cards on the page.")

    matched = {}
    for item in cards_data:
        text_lower = item["rawText"].lower()

        # Must mention a cruise line or ship
        matched_partner = next((p for p in TARGET_CRUISE_KEYWORDS if p in text_lower), None)
        if not matched_partner:
            continue

        # Skip generic navigation elements
        if any(ign in text_lower for ign in GENERIC_IGNORE) and len(item["lines"]) <= 2:
            continue

        # Isolate specific reward title
        specific_title = ""
        for line in item["lines"]:
            l_lower = line.lower()
            if any(k in l_lower for k in ["cruise for two", "night", "spectrum", "anthem", "voyager", "caribbean", "norwegian", "virgin"]):
                specific_title = line
                break
        
        if not specific_title:
            specific_title = item["lines"][0]

        # Extract loyalty point cost if displayed
        points = ""
        for line in item["lines"]:
            if any(char.isdigit() for char in line) and any(kw in line.lower() for kw in ["pts", "points", "lp", ","]):
                points = line
                break

        # Associate partner name
        if "royal caribbean" in text_lower or "spectrum" in text_lower or "anthem" in text_lower:
            partner_name = "Royal Caribbean"
        elif "norwegian" in text_lower or "ncl" in text_lower:
            partner_name = "Norwegian Cruise Line"
        elif "virgin" in text_lower or "lady" in text_lower:
            partner_name = "Virgin Voyages"
        else:
            partner_name = matched_partner.title()

        # Build unique stable identifier
        reward_id = item["href"] if item["href"] else f"{partner_name}_{specific_title}"
        link = f"https://myvip.co{item['href']}" if item["href"].startswith("/") else (item["href"] or "https://myvip.co/rewardstore")

        matched[reward_id] = {
            "title": specific_title,
            "partner": partner_name,
            "available": item["is_available"],
            "points": points,
            "details": " • ".join(item["lines"][:4]),
            "link": link
        }

    return matched

def main():
    cached = load_cache()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()
        current = check_rewards(page)

    print(f"\nFiltered down to {len(current)} specific cruise rewards:")
    for cid, data in current.items():
        print(f" -> [{data['partner']}] {data['title']} | Points: {data['points']} | Avail: {data['available']}")

    alerts_sent = 0
    for r_id, info in current.items():
        # Case 1: Brand-new reward discovered in the store
        if r_id not in cached:
            status = "✅ AVAILABLE" if info["available"] else "❌ SOLD OUT"
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status=f"✨ NEW DROP ({status})",
                link=info["link"],
                points=info["points"],
                details=info["details"]
            )
            alerts_sent += 1

        # Case 2: Restock detected (previously sold out -> now available)
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="🚨 RESTOCKED & AVAILABLE NOW!",
                link=info["link"],
                points=info["points"],
                details=info["details"]
            )
            alerts_sent += 1

        # Case 3: Reward sold out (previously available -> now sold out)
        elif cached[r_id]["available"] and not info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="⚠️ RECENTLY SOLD OUT",
                link=info["link"],
                points=info["points"],
                details=info["details"]
            )
            alerts_sent += 1

    save_cache(current)
    print(f"\nFinished. Sent {alerts_sent} alerts.")

if __name__ == "__main__":
    main()
