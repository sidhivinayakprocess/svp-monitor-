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

# We reverted back to the main dashboard so it actively searches and selects your industry
DASHBOARD_URL = "https://cems.cpcb.gov.in/public/#/l/realtime-connectivity-status-dashboard"
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0
DELAY_THRESHOLD_HOURS = 1.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

def send_telegram(message: str, image_path: str = None):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram keys missing.")
        return
    
    try:
        if image_path and os.path.exists(image_path):
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            with open(image_path, "rb") as photo:
                payload = {"chat_id": CHAT_ID, "caption": message[:1024], "parse_mode": "HTML"}
                requests.post(url, data=payload, files={"photo": photo}, timeout=30)
        else:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
            requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"Telegram notification error: {e}")

def fetch_indian_proxies():
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=IN&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http&country=IN",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
    ]
    proxies = set()
    print("Scraping active Indian HTTP proxies...")
    for src in sources:
        try:
            r = requests.get(src, timeout=8)
            if r.status_code == 200:
                for match in re.finditer(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b", r.text):
                    proxies.add(f"http://{match.group(0)}")
        except:
            continue
    return list(proxies)

def test_proxy_handshake(proxy_url):
    try:
        r = requests.get("https://cems.cpcb.gov.in/public/", proxies={"http": proxy_url, "https": proxy_url}, timeout=8, verify=False)
        if r.status_code == 200:
            return proxy_url
    except:
        pass
    return None

def get_verified_proxies(max_needed=8):
    candidates = fetch_indian_proxies()
    print(f"Testing {len(candidates)} candidate proxies against CPCB firewall...")
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
        futures = {executor.submit(test_proxy_handshake, p): p for p in candidates}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                verified.append(res)
                print(f"Verified live Indian Gateway: {res}")
                if len(verified) >= max_needed:
                    break
    return verified

def block_heavy_assets(route):
    # Allow images so the snapshot looks correct, block everything else heavy
    if route.request.resource_type in ["media", "font"]:
        route.abort()
    else:
        route.continue_()

def run_inspection():
    proxies = get_verified_proxies(max_needed=8)
    if not proxies:
        proxies = [None]

    for attempt, proxy in enumerate(proxies, 1):
        print(f"\n==================================================")
        print(f"ATTEMPT {attempt}/{len(proxies)} -> Proxy: {proxy}")
        print(f"==================================================")

        with sync_playwright() as p:
            launch_args = [
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--ignore-certificate-errors", "--disable-http2", "--window-size=1920,1080",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials"
            ]
            launch_opts = {"headless": True, "args": launch_args}
            if proxy:
                launch_opts["proxy"] = {"server": proxy}

            try:
                browser = p.chromium.launch(**launch_opts)
            except Exception as e:
                print(f"Browser launch failed: {e}")
                continue

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = context.new_page()
            page.set_default_timeout(180000)
            page.route("**/*", block_heavy_assets)

            try:
                # 1. Open Dashboard
                print("Navigating to CPCB Dashboard...")
                page.goto(DASHBOARD_URL, wait_until="commit", timeout=180000)

                # 2. Wait for search box & Search for Industry
                print("Searching for industry...")
                page.wait_for_selector("input:not([readonly]):not([disabled])", timeout=45000)
                page.wait_for_timeout(3000)
                
                inputs = page.locator("input:not([readonly]):not([disabled])").all()
                for inp in inputs:
                    if inp.is_visible():
                        inp.click()
                        inp.fill("")
                        inp.type(TARGET_INDUSTRY, delay=50)
                        page.keyboard.press("Enter")
                        break
                
                page.wait_for_timeout(4000)

                # 3. Click the Action Eye icon to open the data card
                print("Opening industry data card...")
                page.evaluate("""() => {
                    const eye = document.querySelector('.anticon-eye, [nztype="eye"], [data-icon="eye"], svg.ant-icon-eye');
                    if (eye) { (eye.closest('button, a') || eye).click(); return; }
                    const rows = Array.from(document.querySelectorAll('tbody tr')).filter(r => r.textContent.toUpperCase().includes('SIDDHI VINAYAK'));
                    if (rows.length > 0) {
                        const cell = rows[0].querySelector('td:last-child');
                        if (cell) { (cell.querySelector('button, a, i, svg') || cell).click(); }
                    }
                }""")
                page.wait_for_timeout(4000)

                # 4. Click Emission Tab
                print("Switching to Emission tab...")
                page.evaluate("""() => {
                    const tabs = Array.from(document.querySelectorAll('button, a, div, span, [role="tab"], .ant-tabs-tab'));
                    const emissionTab = tabs.find(el => el.textContent.trim().toUpperCase().includes('EMISSION'));
                    if (emissionTab) { emissionTab.click(); }
                }""")

                # 5. WAIT FOR ACTUAL DATA TO RENDER BEFORE SCREENSHOT
                print("Waiting for mg/m³ readings to physically render on the screen...")
                try:
                    # This physically freezes the script until the numbers load in the UI
                    page.wait_for_function("document.body.innerText.toLowerCase().includes('mg/m')", timeout=45000)
                    page.wait_for_timeout(2000) # Give charts an extra 2 seconds to paint
                except Exception as wait_err:
                    print(f"Data render wait timeout. Taking fallback screenshot: {wait_err}")

                # 6. Take Snapshot
                print("Taking a snapshot of the dashboard...")
                screenshot_path = "dashboard_snapshot.png"
                try:
                    page.screenshot(path=screenshot_path, timeout=15000)
                except Exception as e:
                    print(f"Screenshot capture failed: {e}")
                    screenshot_path = None

                # 7. Extract Text
                print("Extracting parameters...")
                page_full_text = page.inner_text("body")
                upper_body = page_full_text.upper()
                alert_reasons = []

                routine_val = "NA"
                routine_time = "Unknown"

                # Condition 1: Number Extraction (Pulls main reading, avoids prescribed limits)
                emission_numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)", page_full_text, re.IGNORECASE)

                if emission_numbers:
                    val = float(emission_numbers[0])
                    routine_val = f"{val} mg/m³" 
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Emission reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")
                else:
                    # ONLY check for "NA" if no numbers exist on the screen
                    is_na_detected = any(pattern in upper_body for pattern in [
                        " NA\n", " NA ", "\nNA\n", "N/A", "N.A", "DATA NOT AVAILABLE"
                    ])
                    if is_na_detected:
                        alert_reasons.append("• Reading is reported as <b>NA</b>")
                        routine_val = "NA (Data Not Available)"
                    else:
                        raise Exception("Target industry data not found on screen. Moving to next proxy.")

                # Condition 2: Timestamp Parsing (Updated to perfectly match your screenshot)
                now_ist = datetime.now(IST)
                # Looks specifically for the text following "Last Received:"
                timestamp_match = re.search(r"Last Received:\s*(.*?)(?:\n|$)", page_full_text, re.IGNORECASE)

                if timestamp_match:
                    raw_ts_str = timestamp_match.group(1).strip()
                    try:
                        # parser automatically handles '06 Oct 2026, 11:15 AM' beautifully
                        parsed_dt = parser.parse(raw_ts_str)
                        last_received_ist = IST.localize(parsed_dt) if parsed_dt.tzinfo is None else parsed_dt.astimezone(IST)
                        routine_time = last_received_ist.strftime('%d %b %Y, %I:%M %p')
                        
                        time_diff = now_ist - last_received_ist
                        delay_hours = time_diff.total_seconds() / 3600.0

                        print(f"Current IST: {now_ist.strftime('%Y-%m-%d %H:%M')}")
                        print(f"Last Received: {last_received_ist.strftime('%Y-%m-%d %H:%M')} (Delay: {delay_hours:.2f} hrs)")

                        if delay_hours >= DELAY_THRESHOLD_HOURS:
                            h = int(time_diff.total_seconds() // 3600)
                            m = int((time_diff.total_seconds() % 3600) // 60)
                            alert_reasons.append(f"• Telemetry delayed by <b>{h}h {m}m</b> (Last received: {routine_time})")
                    except Exception as parse_err:
                        print(f"Timestamp parsing error: {parse_err}")
                else:
                    print("Timestamp string not detected in extracted text.")

                # ---------------------------------------------------------
                # ROUTINE TRIGGER LOGIC (11 AM, 3 PM, 9:30 PM)
                # ---------------------------------------------------------
                is_scheduled = (now_ist.hour == 11 and now_ist.minute < 30) or \
                               (now_ist.hour == 15 and now_ist.minute < 30) or \
                               (now_ist.hour == 21 and now_ist.minute >= 30)

                if alert_reasons or is_scheduled:
                    alert_time_str = now_ist.strftime("%I:%M %p (%d %b %Y)")
                    header = "⚠️ <b>CPCB Emission Alert</b>" if alert_reasons else "📊 <b>Scheduled Routine Update</b>"
                    
                    triggers_text = ""
                    if alert_reasons:
                        reasons_text = "\n".join(alert_reasons)
                        triggers_text = f"<b>Triggers:</b>\n{reasons_text}\n\n"
                        
                    telegram_msg = (
                        f"{header}\n\n"
                        f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                        f"<b>Current Reading:</b> {routine_val}\n"
                        f"<b>Last Received:</b> {routine_time}\n\n"
                        f"{triggers_text}"
                        f"🕒 <b>Report Time:</b> {alert_time_str}\n"
                        f"🔗 <a href='{DASHBOARD_URL}'>Open CPCB Dashboard</a>"
                    )
                    
                    send_telegram(telegram_msg, image_path=screenshot_path)
                    print(f"Telegram notification sent WITH snapshot! (Alert: {bool(alert_reasons)}, Scheduled: {is_scheduled})")
                else:
                    print("Status normal. All conditions within acceptable thresholds.")

                return

            except Exception as e:
                print(f"Iteration attempt failed on proxy {proxy}: {str(e)[:250]}")
            finally:
                browser.close()

    print("\nAll gateway proxies were exhausted during this cycle. The scheduler will retry automatically in 30 minutes.")

if __name__ == "__main__":
    run_inspection()
