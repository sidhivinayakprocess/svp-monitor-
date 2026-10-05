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
            "--ignore-certificate-errors",
            "--window-size=1920,1080"
        ]
        launch_kwargs = {"headless": True, "args": launch_args}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ignore_https_errors=True,
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            print("Navigating to CPCB dashboard...")
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
            
            # 1. Wait longer for proxy loading
            print("Waiting for the table to load over the proxy (this can take up to 20 seconds)...")
            try:
                page.wait_for_selector("table, .ag-root-wrapper, .mat-table, [role='grid']", timeout=45000)
                print("Table grid detected!")
            except:
                print("Warning: Table grid didn't explicitly load within 45 seconds, continuing anyway.")

            page.wait_for_timeout(10000)

            # 2. Advanced Search Input Handling
            search_box = page.locator("input[placeholder*='Search' i], input[type='search'], [aria-label*='search' i]").first
            if search_box.is_visible(timeout=5000):
                print(f"Typing '{TARGET_INDUSTRY}' into search box...")
                search_box.click()
                search_box.fill("") 
                # Mimics human typing character-by-character to trigger Angular's filter
                search_box.type(TARGET_INDUSTRY, delay=100)
                page.keyboard.press("Enter")
                
                print("Waiting 10 seconds for results to filter...")
                page.wait_for_timeout(10000)
            else:
                print("No search box found, checking current screen.")

            # 3. Locate Row (flexible selector for standard tables or ag-grid/material tables)
            row = page.locator("tr, .mat-row, .ag-row, [role='row']", has_text=re.compile(TARGET_INDUSTRY, re.IGNORECASE)).first
            if row.count() == 0:
                print("Row not found. Dumping visible page text for debugging:")
                # Prints out text of body so we can see what actually loaded
                print("-" * 40)
                print(page.inner_text("body")[:1000])
                print("-" * 40)
                return

            print("Found the industry row!")
            row_text = row.inner_text()
            
            # 4. Click the eye button
            print("Clicking eye icon...")
            eye_btn = row.locator("button:has(.fa-eye), a:has(.fa-eye), i.fa-eye, [title*='View' i], svg").first
            eye_btn.click(timeout=10000)
            page.wait_for_timeout(6000)

            # 5. Click Emission Tab
            print("Switching to Emission tab...")
            emission_tab = page.locator("button:has-text('Emission'), [role='tab']:has-text('Emission'), a:has-text('Emission')").first
            if emission_tab.is_visible(timeout=5000):
                emission_tab.click()
                page.wait_for_timeout(4000)

            # 6. Read Text
            modal = page.locator(".modal-content, [role='dialog'], .drawer, .card").first
            modal_text = modal.inner_text() if modal.count() > 0 else page.inner_text("body")
            full_text = f"{row_text}\n{modal_text}"
            
            # --- Condition Evaluation ---
            upper_text = modal_text.upper()
            is_na = any(term in upper_text for term in [" NA\n", " NA ", "N/A", "N.A", "DATA NOT AVAILABLE", "NOT AVAILABLE", "\nNA\n"])
            numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)?", modal_text, re.IGNORECASE)

            alert_reasons = []

            if is_na:
                alert_reasons.append("• Reading is reported as <b>NA</b>")
            elif numbers:
                val = float(numbers[0])
                if val < THRESHOLD:
                    alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                else:
                    print(f"Reading normal: {val} mg/m³")

            time_match = re.search(r"Last received:\s*([0-9a-zA-Z\s/:-]+)", full_text, re.IGNORECASE)
            if time_match:
                raw_time_str = time_match.group(1).strip()
                try:
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
            print("Dumping current page text to help troubleshoot:")
            try:
                print(page.inner_text("body")[:1000])
            except:
                pass
        finally:
            browser.close()

if __name__ == "__main__":
    check_cpcb()
