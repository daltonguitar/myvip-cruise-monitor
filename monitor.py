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
            embed["fields"].append({"name": "Loyalty Points", "value": str(points), "inline": True})

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

def extract_rewards_from_api_payload(payload):
    """Recursively searches JSON responses for reward objects containing inventory flags."""
    extracted = []
    
    def search(obj):
        if isinstance(obj, dict):
            # Typical myVIP / playSTUDIOS reward data structure
            has_name = "name" in obj or "title" in obj
            has_stock = any(k in obj for k in ["quantity_remaining", "inventory", "is_sold_out", "sold_out", "purchasable", "quantityRemaining"])
            if has_name and has_stock:
                name = obj.get("name") or obj.get("title") or ""
                
                # Determine stock strictly
                is_sold = False
                if "is_sold_out" in obj:
                    is_sold = bool(obj["is_sold_out"])
                elif "sold_out" in obj:
                    is_sold = bool(obj["sold_out"])
                elif "quantity_remaining" in obj:
                    is_sold = (obj["quantity_remaining"] == 0)
                elif "quantityRemaining" in obj:
                    is_sold = (obj["quantityRemaining"] == 0)
                elif "purchasable" in obj:
                    is_sold = not bool(obj["purchasable"])

                points = obj.get("points") or obj.get("cost") or obj.get("loyalty_points") or ""
                reward_id = str(obj.get("id") or obj.get("reward_id") or name)

                extracted.append({
                    "id": reward_id,
                    "title": name.strip(),
                    "available": not is_sold,
                    "points": points
                })
            else:
                for v in obj.values():
                    search(v)
        elif isinstance(obj, list):
            for item in obj:
                search(item)

    search(payload)
    return extracted

def scrape_partner_fast(page, partner_info: dict) -> dict:
    url = partner_info["url"]
    partner_name = partner_info["name"]
    print(f"\n[Fast Scan] Navigating to {partner_name} ({url})...")

    intercepted_items = []

    def on_response(response):
        # Capture internal JSON responses
        try:
            if "application/json" in response.headers.get("content-type", ""):
                data = response.json()
                items = extract_rewards_from_api_payload(data)
                if items:
                    intercepted_items.extend(items)
        except Exception:
            pass

    page.on("response", on_response)

    # Load page and scroll once to trigger catalog load
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    # Click See More once or twice max if still loading
    for _ in range(3):
        try:
            btn = page.locator("button:has-text('See More'), button:has-text('Load More')").first
            if btn.is_visible(timeout=1000):
                btn.click()
                time.sleep(1.5)
            else:
                break
        except Exception:
            break

    # Build the dictionary of rewards
    results = {}
    
    # 1. If backend API data was intercepted, use it (highest accuracy)
    if intercepted_items:
        print(f"Captured {len(intercepted_items)} rewards via backend network payload.")
        for item in intercepted_items:
            key = f"{partner_name}_{item['title']}"
            results[key] = {
                "title": item["title"],
                "partner": partner_name,
                "available": item["available"],
                "points": item["points"],
                "link": url
            }
    
    # 2. Fallback: Parse visible cards from DOM in one fast batch without clicking
    if not results:
        print("API payload not found; using rapid single-pass DOM evaluation...")
        dom_cards = page.evaluate("""() => {
            const cards = document.querySelectorAll('div[class*="Card"], div[class*="card"], a[href*="/reward/"]');
            return Array.from(cards).map(card => {
                const text = card.innerText || '';
                const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                const isSoldOut = /sold out|out of stock|unavailable/i.test(text);
                const hasDisabled = card.getAttribute('aria-disabled') === 'true' || card.querySelector('[aria-disabled="true"]') !== null;
                return {
                    lines: lines,
                    available: !(isSoldOut || hasDisabled)
                };
            }).filter(c => c.lines.length >= 2);
        }""")

        for c in dom_cards:
            title = c["lines"][0]
            if title.lower() in ["norwegian", "royal caribbean"] and len(c["lines"]) > 1:
                title = c["lines"][1]
            key = f"{partner_name}_{title}"
            results[key] = {
                "title": title,
                "partner": partner_name,
                "available": c["available"],
                "points": "",
                "link": url
            }

    return results

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
                rewards = scrape_partner_fast(page, partner)
                current_all.update(rewards)
                print(f"Scanned {len(rewards)} rewards for {partner['name']}.")
            except Exception as e:
                print(f"Error scanning {partner['name']}: {e}")

    print(f"\nTotal tracked rewards: {len(current_all)}")

    alerts_sent = 0
    for r_id, info in current_all.items():
        # Only alert new drops if they are actually in stock
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

        # Restock detected
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="🚨 RESTOCKED & AVAILABLE NOW!",
                link=info["link"],
                points=info["points"]
            )
            alerts_sent += 1

        # Sold out detected
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
    print(f"\nCycle finished in seconds. Sent {alerts_sent} alerts.")

if __name__ == "__main__":
    main()
