import os
import re
import sys
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "https://cems.cpcb.gov.in/public/#/l/realtime-connectivity-status-dashboard"
BASE_URL = "https://cems.cpcb.gov.in/public/"
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=20)
        print(f"Telegram API status: {res.status_code}")
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")

def test_raw_connection():
    """Quick diagnostic to verify if CPCB firewall blocks GitHub cloud IPs."""
    print("Testing raw network connectivity to CPCB...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(BASE_URL, headers=headers, timeout=15, verify=False)
        print(f"Raw connection SUCCESS: HTTP {resp.status_code}")
        return True
    except requests.exceptions.ConnectTimeout:
        print("Raw connection FAILED: Connection Timed Out.")
        print("CPCB firewall is dropping packets from GitHub/Azure cloud IPs (Geo/Datacenter Block).")
        return False
    except Exception as ex:
        print(f"Raw connection failed with error: {ex}")
        return False

def check_cpcb():
    # Run diagnostic first
    is_reachable = test_raw_connection()
    if not is_reachable:
        send_telegram(
            "⚠️ <b>CPCB Monitor Notice</b>\n\n"
            "GitHub's cloud servers are blocked by CPCB's firewall (Geo/IP restriction). "
            "Switching to a mobile-run solution is required."
        )
        sys.exit(1)

    print(f"Checking CPCB portal for {TARGET_INDUSTRY}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        try:
            # wait_until='commit' returns as soon as the first byte of data is received
            print("Navigating to CPCB dashboard...")
            page.goto(DASHBOARD_URL, wait_until="commit", timeout=45000)
            
            print("Initial response received. Waiting for page components to render...")
            page.wait_for_timeout(10000)

            # Search for the target industry
            search_box = page.locator("input[placeholder*='Search' i], input[type='search'], input[aria-label*='Search' i]").first
            if search_box.is_visible(timeout=5000):
                print(f"Filtering for '{TARGET_INDUSTRY}'...")
                search_box.fill(TARGET_INDUSTRY)
                page.keyboard.press("Enter")
                page.wait_for_timeout(4000)

            # Locate the industry row
            row = page.locator("tr", has_text=TARGET_INDUSTRY).first
            if row.count() == 0:
                print(f"Could not locate row for {TARGET_INDUSTRY}. Page content:")
                print(page.inner_text("body")[:300])
                return

            print("Found industry row. Clicking eye icon...")
            eye_btn = row.locator("button:has(.fa-eye), a:has(.fa-eye), i.fa-eye, [title*='View' i], svg").first
            eye_btn.click()
            page.wait_for_timeout(4000)

            # Click 'Emission' tab
            print("Opening 'Emission' panel...")
            emission_tab = page.locator("button:has-text('Emission'), [role='tab']:has-text('Emission'), a:has-text('Emission')").first
            if emission_tab.is_visible(timeout=5000):
                emission_tab.click()
                page.wait_for_timeout(3000)

            # Read text inside modal
            modal = page.locator(".modal-content, [role='dialog'], .drawer, .card").first
            modal_text = modal.inner_text() if modal.count() > 0 else page.inner_text("body")
            print(f"Captured data preview:\n{modal_text[:300]}")

            upper_text = modal_text.upper()
            is_na = any(term in upper_text for term in [" NA", "N/A", "N.A", "DATA NOT AVAILABLE", "NOT AVAILABLE"])
            numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)?", modal_text, re.IGNORECASE)

            should_alert = False
            reason = ""
            reading_display = "Unknown"

            if is_na:
                should_alert = True
                reason = "Reading reported as NA"
                reading_display = "NA"
            elif numbers:
                val = float(numbers[0])
                reading_display = f"{val} mg/m³"
                if val < THRESHOLD:
                    should_alert = True
                    reason = f"Emission value ({val} mg/m³) is below {THRESHOLD} mg/m³"
                else:
                    print(f"Reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")
            else:
                should_alert = True
                reason = "Value empty or unreadable"
                reading_display = "Empty / Unreadable"

            if should_alert:
                now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
                msg = (
                    f"⚠️ <b>CPCB Emission Alert</b>\n\n"
                    f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n"
                    f"📊 <b>Condition:</b> {reason}\n"
                    f"⏱ <b>Recorded Value:</b> {reading_display}\n"
                    f"🕒 <b>Checked At:</b> {now_str}\n"
                    f"🔗 <a href='{DASHBOARD_URL}'>CPCB Portal</a>"
                )
                send_telegram(msg)
            else:
                print("No alert required.")

        except Exception as e:
            print(f"Error during extraction: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    check_cpcb()
