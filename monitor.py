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
            embed["fields"].append({"name": "Stock", "value": stock_text, "inline": True})

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
    
    # Wait specifically for the carousel elements and badge ribbons to mount
    try:
        page.wait_for_selector("text=/LEFT|SOLD OUT/i", timeout=12000)
        print("Ribbon badges detected on page!")
    except Exception:
        print("Timeout waiting for explicit badge text, proceeding with render scan...")

    time.sleep(3)

    # Scroll down to make sure all carousels/sections trigger their renders
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3);")
    time.sleep(1)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 1.5);")
    time.sleep(1)

    cards_data = page.evaluate("""() => {
        const results = [];
        // Catch all card elements across carousels and grids
        const cards = document.querySelectorAll('div[class*="Card"], div[class*="card"], div[class*="slide"], a[href*="/reward/"]');

        cards.forEach(card => {
            const rawText = (card.innerText || '').trim();
            const lines = rawText.split('\\n').map(l => l.trim()).filter(Boolean);
            if (lines.length < 2 || rawText.length > 500) return;

            // Search every child node specifically for ribbon badge text
            let badgeText = "";
            const allElements = [card, ...Array.from(card.querySelectorAll('*'))];
            for (const el of allElements) {
                const text = (el.innerText || '').trim().toUpperCase();
                // Match ribbon patterns: "4 LEFT", "3 LEFT", "SOLD OUT"
                if (text === 'SOLD OUT' || /\\d+\\s+LEFT/.test(text)) {
                    badgeText = text;
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
        if any(h in lines[0].lower() for h in ["all partners", "terms", "back", "reward store", "travel at"]):
            continue

        # Clean title
        title = lines[0]
        if title.upper() in ["SOLD OUT"] or "LEFT" in title.upper():
            title = lines[1] if len(lines) > 1 else title

        if title.lower() in ["norwegian", "royal caribbean", "virgin voyages"] and len(lines) > 1:
            title = lines[1]

        # Ignore non-reward navigational artifacts
        if len(title) < 4:
            continue

        badge = item["badgeText"].upper()
        if not badge:
            # Fallback text scan on lines
            for l in lines:
                up = l.upper()
                if up == "SOLD OUT" or "LEFT" in up:
                    badge = up
                    break

        is_cruise_sailing = any(k in title.lower() for k in ["night", "cruise", "sailing", "voyage"])

        if badge == "SOLD OUT" or "OUT OF STOCK" in badge:
            is_available = False
            status_desc = "SOLD OUT"
        elif "LEFT" in badge:
            is_available = True
            status_desc = badge  # e.g., "4 LEFT", "3 LEFT"
        else:
            if is_cruise_sailing:
                # If it's a cruise sailing and has no "LEFT" badge, it's sold out
                is_available = False
                status_desc = "SOLD OUT"
            else:
                # Digital vouchers (FreePlay, discount codes) don't have cabin counts
                is_available = True
                status_desc = "IN STOCK"

        reward_id = f"{partner_name}_{title}"
        link = f"https://myvip.co{item['href']}" if item["href"].startswith("/") else (item["href"] or url)

        # Deduplicate cards that appear in multiple carousel wrappers
        if reward_id not in partner_rewards or partner_rewards[reward_id]["stock_text"] == "SOLD OUT":
            partner_rewards[reward_id] = {
                "title": title,
                "partner": partner_name,
                "available": is_available,
                "stock_text": status_desc,
                "link": link
            }

    for cid, data in partner_rewards.items():
        print(f" -> [{data['partner']}] {data['title']} | Badge: '{data['stock_text']}' | Available: {data['available']}")

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

    print(f"\nTotal unique items tracked: {len(current_all)}")

    alerts_sent = 0
    for r_id, info in current_all.items():
        # First discovery: only alert if actually in stock
        if r_id not in cached:
            if info["available"]:
                send_alert(
                    title=info["title"],
                    partner=info["partner"],
                    status=f"✨ AVAILABLE NOW ({info['stock_text']})",
                    link=info["link"],
                    stock_text=info["stock_text"]
                )
                alerts_sent += 1
            else:
                print(f"Initial sync: cataloged sold-out sailing: {info['title']}")

        # Restock: was False -> now True
        elif not cached[r_id]["available"] and info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status=f"🚨 RESTOCKED & AVAILABLE NOW! ({info['stock_text']})",
                link=info["link"],
                stock_text=info["stock_text"]
            )
            alerts_sent += 1

        # Sold Out: was True -> now False
        elif cached[r_id]["available"] and not info["available"]:
            send_alert(
                title=info["title"],
                partner=info["partner"],
                status="⚠️ SOLD OUT",
                link=info["link"],
                stock_text=info["stock_text"]
            )
            alerts_sent += 1

    save_cache(current_all)
    print(f"Finished. Alerts sent: {alerts_sent}")

if __name__ == "__main__":
    main()
