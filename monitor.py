import os
import re
import concurrent.futures
from datetime import datetime
import requests
import pytz
from dateutil import parser
from playwright.sync_api import sync_playwright
import urllib3
urllib3.disable_warnings()

DASHBOARD_URL = "https://cems.cpcb.gov.in/public/#/l/realtime-connectivity-status-dashboard"
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")
stop_testing = False

def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_indian_proxies():
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=IN",
        "https://www.proxy-list.download/api/v1/get?type=http&country=IN",
        "https://www.proxy-list.download/api/v1/get?type=https&country=IN"
    ]
    proxies = set()
    print("Fetching live Indian proxies to bypass firewall...")
    for url in sources:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                for match in re.finditer(r"\d{1,3}(?:\.\d{1,3}){3}:\d+", r.text):
                    proxies.add(match.group(0))
        except:
            pass
    return list(proxies)

def test_proxy(proxy):
    global stop_testing
    if stop_testing: return None
    proxy_url = f"http://{proxy}"
    try:
        r = requests.get(
            "https://cems.cpcb.gov.in/public/",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=8,
            verify=False
        )
        if r.status_code in [200, 301, 302, 401, 403]:
            return proxy_url
    except:
        pass
    return None

def get_working_proxy():
    global stop_testing
    proxies = fetch_indian_proxies()
    print(f"Testing {len(proxies)} proxies concurrently...")
    
    stop_testing = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(test_proxy, p) for p in proxies]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                stop_testing = True
                print(f"Success! Connecting via: {res}")
                return res
    return None

def check_cpcb():
    proxy = get_working_proxy()
    
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

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ignore_https_errors=True
        )
        page = context.new_page()

        try:
            print("Navigating to CPCB dashboard...")
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)

            search_box = page.locator("input[placeholder*='Search' i], input[type='search']").first
            if search_box.is_visible(timeout=5000):
                search_box.fill(TARGET_INDUSTRY)
                page.keyboard.press("Enter")
                page.wait_for_timeout(4000)

            row = page.locator("tr", has_text=TARGET_INDUSTRY).first
            if row.count() == 0:
                print("Row not found.")
                return

            row_text = row.inner_text()
            eye_btn = row.locator("button:has(.fa-eye), a:has(.fa-eye), i.fa-eye, [title*='View' i], svg").first
            eye_btn.click()
            page.wait_for_timeout(4000)

            emission_tab = page.locator("button:has-text('Emission'), [role='tab']:has-text('Emission'), a:has-text('Emission')").first
            if emission_tab.is_visible(timeout=5000):
                emission_tab.click()
                page.wait_for_timeout(4000)

            modal = page.locator(".modal-content, [role='dialog'], .drawer, .card").first
            modal_text = modal.inner_text() if modal.count() > 0 else page.inner_text("body")
            full_text = f"{row_text}\n{modal_text}"
            
            # --- Condition Evaluation ---
            upper_text = modal_text.upper()
            is_na = any(term in upper_text for term in [" NA", "N/A", "N.A", "DATA NOT AVAILABLE", "NOT AVAILABLE"])
            numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)?", modal_text, re.IGNORECASE)

            alert_reasons = []

            # Cond 1: NA
            if is_na:
                alert_reasons.append("• Reading is reported as <b>NA</b>")
            
            # Cond 2: < 5 mg/m3
            elif numbers:
                val = float(numbers[0])
                if val < THRESHOLD:
                    alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                else:
                    print(f"Reading normal: {val} mg/m³")

            # Cond 3: Last Received Delay >= 1 Hour
            time_match = re.search(r"Last received:\s*([0-9a-zA-Z\s/:-]+)", full_text, re.IGNORECASE)
            if time_match:
                raw_time_str = time_match.group(1).strip()
                try:
                    # Translate CPCB time into Indian Standard Time and compare against Current IST
                    last_time_naive = parser.parse(raw_time_str, fuzzy=True, dayfirst=True)
                    last_time_ist = IST.localize(last_time_naive)
                    now_ist = datetime.now(IST)
                    
                    time_diff = now_ist - last_time_ist
                    delay_hours = time_diff.total_seconds() / 3600
                    
                    print(f"Parsed Time: {last_time_ist.strftime('%d %b %H:%M')} | Delay: {delay_hours:.2f} hours")

                    if delay_hours >= 1.0:
                        hrs = int(time_diff.total_seconds() // 3600)
                        mins = int((time_diff.total_seconds() % 3600) // 60)
                        alert_reasons.append(f"• Telemetry is delayed by <b>{hrs}h {mins}m</b> (Last: {last_time_ist.strftime('%d %b %I:%M %p')})")
                except Exception as e:
                    print(f"Could not parse timestamp '{raw_time_str}': {e}")

            # Dispatch Alert
            if alert_reasons:
                reasons_str = "\n".join(alert_reasons)
                now_str = datetime.now(IST).strftime("%I:%M %p")
                
                msg = (
                    f"⚠️ <b>CPCB Emission Alert</b>\n\n"
                    f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                    f"<b>Triggers:</b>\n{reasons_str}\n\n"
                    f"🕒 <b>Alert Time:</b> {now_str}\n"
                    f"🔗 <a href='{DASHBOARD_URL}'>Open CPCB Portal</a>"
                )
                send_telegram(msg)
                print("Alert sent.")
            else:
                print("No alert required.")

        except Exception as e:
            print(f"Extraction error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    check_cpcb()
