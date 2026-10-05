import os
import re
import sys
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "https://cems.cpcb.gov.in/public/#/l/realtime-connectivity-status-dashboard"
CHECK_URL = "https://cems.cpcb.gov.in/public/"
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"Telegram notification error: {e}")

def find_working_indian_proxy() -> str:
    """Finds an active Indian proxy to bypass CPCB's geo-blocking firewall."""
    print("Searching for an active Indian proxy...")
    proxy_sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=IN&ssl=all&anonymity=all",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
    ]
    
    candidates = []
    for source in proxy_sources:
        try:
            resp = requests.get(source, timeout=10)
            if resp.status_code == 200:
                lines = [line.strip() for line in resp.text.splitlines() if line.strip() and ":" in line]
                candidates.extend(lines[:15])
        except Exception:
            continue

    print(f"Testing {len(candidates)} proxy candidates against CPCB...")
    for proxy in candidates:
        formatted_proxy = f"http://{proxy}"
        try:
            test_resp = requests.get(
                CHECK_URL,
                proxies={"http": formatted_proxy, "https": formatted_proxy},
                timeout=6,
                verify=False
            )
            if test_resp.status_code in [200, 301, 302]:
                print(f"Connected to CPCB via Indian proxy: {proxy}")
                return formatted_proxy
        except Exception:
            continue
            
    print("No responsive public proxy found in this cycle.")
    return ""

def check_cpcb():
    proxy = find_working_indian_proxy()
    
    with sync_playwright() as p:
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--ignore-certificate-errors"
        ]
        
        launch_kwargs = {"headless": True, "args": launch_args}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
            print(f"Launching Chromium routed through {proxy}")
        else:
            print("Attempting direct connection...")

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=True
        )
        page = context.new_page()

        try:
            print("Navigating to CPCB dashboard...")
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

            # Search industry
            search_box = page.locator("input[placeholder*='Search' i], input[type='search']").first
            if search_box.is_visible(timeout=5000):
                search_box.fill(TARGET_INDUSTRY)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3000)

            # Find row
            row = page.locator("tr", has_text=TARGET_INDUSTRY).first
            if row.count() == 0:
                print(f"Row for {TARGET_INDUSTRY} not visible yet.")
                return

            # Click eye icon
            eye_btn = row.locator("button:has(.fa-eye), a:has(.fa-eye), i.fa-eye, [title*='View' i], svg").first
            eye_btn.click()
            page.wait_for_timeout(3000)

            # Click Emission tab
            emission_tab = page.locator("button:has-text('Emission'), [role='tab']:has-text('Emission'), a:has-text('Emission')").first
            if emission_tab.is_visible(timeout=5000):
                emission_tab.click()
                page.wait_for_timeout(3000)

            # Read modal text
            modal = page.locator(".modal-content, [role='dialog'], .drawer, .card").first
            modal_text = modal.inner_text() if modal.count() > 0 else page.inner_text("body")
            print(f"Captured text:\n{modal_text[:200]}")

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
                print("Alert sent to Telegram successfully.")

        except Exception as e:
            print(f"Extraction error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    check_cpcb()
