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

# The Golden URL you found! Jumps directly to Siddhi Vinayak Process
DIRECT_URL = "https://cems.cpcb.gov.in/public/#/l/dashboard/site-info/eyJvYmoiOiJpbmR1c3RyeV83MjU3In0="
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

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

def get_working_proxies(limit=4):
    proxies = fetch_indian_proxies()
    print(f"Testing {len(proxies)} proxies concurrently...")
    
    working_proxies = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(test_proxy, p): p for p in proxies}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                working_proxies.append(res)
                print(f"Verified active proxy: {res}")
                if len(working_proxies) >= limit:
                    break
    return working_proxies

def block_heavy_resources(route):
    # Block images/fonts so the proxy loads lightning fast
    if route.request.resource_type in ["image", "media", "font"]:
        route.abort()
    else:
        route.continue_()

def check_cpcb():
    proxy_list = get_working_proxies(limit=4)
    if not proxy_list:
        print("No proxies found, attempting direct fallback...")
        proxy_list = [None]

    for attempt, proxy in enumerate(proxy_list, 1):
        print(f"\n=== ATTEMPT {attempt} using Proxy: {proxy} ===")
        
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
            page.route("**/*", block_heavy_resources)

            try:
                print("Navigating DIRECTLY to Siddhi Vinayak Process portal...")
                # Jump straight to the dashboard using your exact link
                page.goto(DIRECT_URL, wait_until="domcontentloaded", timeout=60000)
                
                print("Waiting for industry panel to hydrate...")
                # Wait for any generic card, tab, or grid layout to appear
                page.wait_for_selector(".ant-card, .ant-tabs, [role='tablist']", timeout=30000)
                page.wait_for_timeout(5000)

                print("Switching to Emission tab...")
                emission_tab = page.locator("button:has-text('Emission'), [role='tab']:has-text('Emission'), a:has-text('Emission'), .ant-tabs-tab:has-text('Emission')").first
                if emission_tab.is_visible(timeout=5000):
                    emission_tab.click()
                    page.wait_for_timeout(4000)
                else:
                    print("Emission tab not explicitly found. It might already be open. Scanning page...")

                # Read all text currently visible on the page
                full_text = page.inner_text("body")
                
                # --- Evaluate Your 3 Conditions ---
                upper_text = full_text.upper()
                is_na = any(term in upper_text for term in [" NA\n", " NA ", "N/A", "N.A", "DATA NOT AVAILABLE", "NOT AVAILABLE", "\nNA\n"])
                
                # Find all numbers attached to mg/m3 or similar units
                numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)?", full_text, re.IGNORECASE)

                alert_reasons = []

                # Condition 1: NA
                if is_na:
                    alert_reasons.append("• Reading is reported as <b>NA</b>")
                # Condition 2: < 5 mg/m3
                elif numbers:
                    val = float(numbers[0])
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Reading normal: {val} mg/m³")

                # Condition 3: Delay > 1 Hour (Format: YYYY-MM-DD HH:MM)
                time_match = re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})", full_text)
                if time_match:
                    raw_time_str = time_match.group(1).strip()
                    try:
                        last_time_naive = parser.parse(raw_time_str)
                        last_time_ist = IST.localize(last_time_naive)
                        now_ist = datetime.now(IST)
                        
                        time_diff = now_ist - last_time_ist
                        delay_hours = time_diff.total_seconds() / 3600
                        
                        print(f"Parsed Time: {last_time_ist.strftime('%Y-%m-%d %H:%M')} | Delay: {delay_hours:.2f} hours")

                        if delay_hours >= 1.0:
                            hrs = int(time_diff.total_seconds() // 3600)
                            mins = int((time_diff.total_seconds() % 3600) // 60)
                            alert_reasons.append(f"• Telemetry is delayed by <b>{hrs}h {mins}m</b> (Last: {last_time_ist.strftime('%d %b %I:%M %p')})")
                    except Exception as e:
                        print(f"Could not parse timestamp '{raw_time_str}': {e}")
                else:
                    print("Warning: Could not locate a valid YYYY-MM-DD timestamp on the page.")

                if alert_reasons:
                    reasons_str = "\n".join(alert_reasons)
                    now_str = datetime.now(IST).strftime("%I:%M %p")
                    msg = (
                        f"⚠️ <b>CPCB Emission Alert</b>\n\n"
                        f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                        f"<b>Triggers:</b>\n{reasons_str}\n\n"
                        f"🕒 <b>Alert Time:</b> {now_str}\n"
                        f"🔗 <a href='{DIRECT_URL}'>Open Direct Portal</a>"
                    )
                    send_telegram(msg)
                    print("Alert sent successfully.")
                else:
                    print("No alert required. Readings are normal.")

                # If we reach this line, the scrape was 100% successful! 
                return 

            except Exception as e:
                print(f"Proxy {proxy} failed during execution: {str(e)[:200]}")
            finally:
                browser.close()
                
    print("\nCRITICAL: All proxies failed to load the data. Will try again on the next 30-minute schedule.")

if __name__ == "__main__":
    check_cpcb()
