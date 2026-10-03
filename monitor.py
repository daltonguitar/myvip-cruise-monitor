import os
import sys
import json
import time
import re
from playwright.sync_api import sync_playwright
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

CACHE_FILE = "rewards_cache.json"

PARTNER_TARGETS = [
    {"name": "Royal Caribbean", "url": "https://myvip.co/rewardstore/partner/29"},
    {"name": "Norwegian Cruise Line", "url": "https://myvip.co/rewardstore/partner/66"},
]

def send_alert(title: str, partner: str, status: str, link: str, stock_text: str = ""):
    print(f"\n[ALERT] {partner} | {title} | {status} ({stock_text})\n")

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
        if stock_text:
            embed["fields"].append({"name": "Stock Badge", "value": stock_text, "inline": True})

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
        tg_text = f"🚢 *{title}*\n*Partner:* {partner}\n*Status:* {status}\n*Stock:* {stock_text or 'N/A'}\n[View Reward]({link})"
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

def scrape_partner_grid(page, partner_info: dict) -> dict:
    url = partner_info["url"]
    partner_name = partner_info["name"]
    print(f"\nScanning {partner_name} at {url} ...")

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)

    # Scroll down to ensure all tiles render
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(1.5)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.5)

    # Click carousel arrows or 'See More' to expose all hidden cards
    for _ in range(5):
        try:
            btn = page.locator("button:has-text('See More'), button:has-text('Load More')").first
            if btn.is_visible(timeout=1000):
                btn.click()
                time.sleep(1.5)
            else:
                break
        except Exception:
            break

    # Read the card content and ribbon badges
    cards_data = page.evaluate("""() => {
        const results = [];
        // Match reward card containers
        const cards = document.querySelectorAll('div[class*="Card"], div[class*="card"], a[href*="/reward/"], div[role="button"]');

        cards.forEach(card => {
            const rawText = (card.innerText || '').trim();
            const lines = rawText.split('\\n').map(l => l.trim()).filter(Boolean);
            if (lines.length < 2 || rawText.length > 500) return;

            // Look for any badge or ribbon text (e.g., 'SOLD OUT', '4 LEFT', '3 LEFT')
            let badgeText = "";
            const allElements = [card, ...Array.from(card.querySelectorAll('*'))];
            for (const el of allElements) {
                const t = (el.innerText || '').trim().toUpperCase();
                if (t === 'SOLD OUT' || /\\d+\\s+LEFT/.test(t) || t === 'OUT OF STOCK') {
                    badgeText = t;
                    break;
                }
            }

            const linkEl = card.tagName === 'A' ? card : card.querySelector('a');
            const href = linkEl ? linkEl.getAttribute('href') : '';

            results.push({
                rawText: rawText,
                lines: lines,
                href: href || '',
                badgeText: badgeText
            });
        });
        return results;
    }""")

    partner_rewards = {}
    for item in cards_data:
        lines = item["lines"]
        if any(h in lines[0].lower() for h in ["all partners", "terms", "back", "reward store", "travel"]):
            continue

        # Isolate the sailing title (skipping the badge text if it appeared as the first line)
        title = lines[0]
        if title.upper() in ["SOLD OUT"] or "LEFT" in title.upper():
            title = lines[1] if len(lines) > 1 else title

        if title.lower() in ["norwegian", "royal caribbean", "virgin voyages"] and len(lines) > 1:
            title = lines[1]

        # Determine stock from the ribbon badge
        badge = item["badgeText"].upper()
        if not badge:
            # Fallback to checking full card text for badge words
            for l in lines:
                up = l.upper()
                if up == "SOLD OUT" or "LEFT" in up:
                    badge = up
                    break

        if badge == "SOLD OUT" or "OUT OF STOCK" in badge:
            is_available = False
            status_desc = "SOLD OUT"
        elif "LEFT" in badge:
            is_available = True
            status_desc = badge  # e.g., "4 LEFT", "3 LEFT"
        else:
            # Default safe: if there is no stock badge at all, treat as sold out
            is_available = False
            status_desc = "NO STOCK BADGE"

        reward_id = f"{partner_name}_{title}"
        link = f"https://myvip.co{item['href']}" if item["href"].startswith("/") else (item["href"] or url)

        partner_rewards[reward_id] = {
            "title": title,
            "partner": partner_name,
            "available": is_available,
            "stock_text": status_desc,
            "link": link
        }
        print(f" -> [{partner_name}] {title} | Status: {status_desc} (Available: {is_available})")

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
                rewards = scrape_partner_grid(page, partner)
                current_all.update(rewards)
            except Exception as e:
                print(f"Error scanning {partner['name']}: {e}")

    print(f"\nTotal items processed: {len(current_all)}")

    alerts_sent = 0
    for r_id, info in current_all.items():
        # Case 1: Brand new drop that is IN STOCK
        if r_id not in cached:
            if info["available"]:
                send_alert(
                    title=info["title"],
                    partner=info["partner"],
                    status="✨ NEW DROP (AVAILABLE)",
                    link=info["link"],
                    stock_text=info["stock_text"]
                )
                alerts_sent += 1
            else:
                print(f"Silently cataloged sold-out item: {info['title']}")

        # Case 2: Restock (was False -> now True)
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="🚨 RESTOCKED & AVAILABLE NOW!",
                link=info["link"],
                stock_text=info["stock_text"]
            )
            alerts_sent += 1

        # Case 3: Sold out (was True -> now False)
        elif cached[r_id]["available"] and not info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="⚠️ RECENTLY SOLD OUT",
                link=info["link"],
                stock_text=info["stock_text"]
            )
            alerts_sent += 1

    save_cache(current_all)
    print(f"Cycle finished. Alerts sent: {alerts_sent}")

if __name__ == "__main__":
    main()
