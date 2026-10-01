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
    # Add Virgin Voyages URL here when available
]

def send_alert(title: str, partner: str, status: str, link: str, points: str = "", details: str = ""):
    print(f"\n[ALERT] {partner} | {title} | {status}\n")

    if DISCORD_WEBHOOK_URL:
        embed_color = 5763719 if "AVAILABLE" in status else 15548997
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

def expand_all_rewards(page):
    """Clicks 'See More', 'Load More', or 'View All' until all items are visible."""
    max_clicks = 8
    for i in range(max_clicks):
        try:
            # Look for common variations of the expand button
            see_more = page.locator("button:has-text('See More'), button:has-text('Load More'), button:has-text('View All'), a:has-text('See More')").first
            if see_more.is_visible(timeout=2000):
                print(f"Clicking 'See More' button (expansion {i+1})...")
                see_more.click()
                time.sleep(2.5)
            else:
                break
        except Exception:
            break

def scrape_partner_page(page, partner_info: dict) -> dict:
    url = partner_info["url"]
    partner_name = partner_info["name"]
    print(f"\nScanning {partner_name} at {url} ...")

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(4)

    # 1. Scroll down to trigger lazy loading
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(1.5)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.5)

    # 2. Click 'See More' to reveal all hidden cards
    expand_all_rewards(page)

    # 3. Final scroll to ensure all newly expanded cards mount
    page.evaluate("window.scrollTo(0, 0);")
    time.sleep(1)

    cards_data = page.evaluate("""() => {
        const results = [];
        // Match reward cards on the partner grid
        const cards = document.querySelectorAll('div[class*="Card"], div[class*="card"], a[href*="/reward/"], div[role="button"]');

        cards.forEach(card => {
            const rawText = (card.innerText || "").trim();
            const lines = rawText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            
            // Skip containers that are too small or whole-page containers
            if (lines.length < 2 || rawText.length > 500) return;

            const linkEl = card.tagName === 'A' ? card : card.querySelector('a');
            const href = linkEl ? linkEl.getAttribute('href') : '';
            
            // 1. Text checks (case-insensitive)
            const textLower = rawText.toLowerCase();
            const textIndicatesSoldOut = textLower.includes('sold out') || 
                                         textLower.includes('out of stock') || 
                                         textLower.includes('unavailable') || 
                                         textLower.includes('0 remaining') ||
                                         textLower.includes('check back');

            // 2. Class checks on the card container and children
            const classString = (card.className || '') + ' ' + Array.from(card.querySelectorAll('*')).map(el => el.className || '').join(' ');
            const classIndicatesSoldOut = /sold-out|soldout|disabled|unavailable|inactive|out-of-stock/i.test(classString);

            // 3. Accessibility / Disabled attribute checks
            const attrIndicatesSoldOut = card.getAttribute('aria-disabled') === 'true' || 
                                         card.hasAttribute('disabled') ||
                                         card.querySelector('[aria-disabled="true"], [disabled]') !== null;

            // 4. Style check (opacity dimming for out of stock)
            const computedStyle = window.getComputedStyle(card);
            const opacity = parseFloat(computedStyle.opacity || '1');
            const isDimmed = opacity < 0.7;

            // An item is sold out if ANY indicator flags it
            const isSoldOut = textIndicatesSoldOut || classIndicatesSoldOut || attrIndicatesSoldOut || isDimmed;

            results.push({
                rawText: rawText,
                lines: lines,
                href: href || '',
                is_available: !isSoldOut
            });
        });
        return results;
    }""")

    partner_rewards = {}
    for item in cards_data:
        lines = item["lines"]
        # Skip generic headers / back navigation
        if any(h in lines[0].lower() for h in ["all partners", "terms", "back", "reward store", "see more"]):
            continue

        title = lines[0]
        # If line 0 is the brand name, take line 1 as the actual sailing title
        if title.lower() in ["norwegian", "royal caribbean", "virgin voyages"] and len(lines) > 1:
            title = lines[1]

        points = ""
        for line in lines:
            if any(char.isdigit() for char in line) and any(kw in line.lower() for kw in ["pts", "points", "lp", ","]):
                points = line
                break

        reward_id = item["href"] if item["href"] else f"{partner_name}_{title}"
        link = f"https://myvip.co{item['href']}" if item["href"].startswith("/") else (item["href"] or url)

        partner_rewards[reward_id] = {
            "title": title,
            "partner": partner_name,
            "available": item["is_available"],
            "points": points,
            "details": " • ".join(lines[:4]),
            "link": link
        }

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
                rewards = scrape_partner_page(page, partner)
                current_all.update(rewards)
                print(f"Scanned {len(rewards)} total rewards for {partner['name']}.")
            except Exception as e:
                print(f"Error scanning {partner['name']}: {e}")

    print(f"\nGrand Total Rewards Tracked: {len(current_all)}")
    for cid, data in current_all.items():
        print(f" -> [{data['partner']}] {data['title']} | Available: {data['available']}")

    alerts_sent = 0
    for r_id, info in current_all.items():
        # Case 1: Brand-new drop
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

        # Case 2: Restock
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

        # Case 3: Sold out transition
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

    save_cache(current_all)
    print(f"\nCycle finished. Sent {alerts_sent} alerts.")

if __name__ == "__main__":
    main()
