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

DIRECT_URL = "https://cems.cpcb.gov.in/public/#/l/dashboard/site-info/eyJvYmoiOiJpbmR1c3RyeV83MjU3In0="
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0
DELAY_THRESHOLD_HOURS = 1.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

def send_telegram(message: str, image_path: str = None):
    """Sends a Telegram message, with an optional photo attachment."""
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
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
        "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"
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
    """STRICT 6-SECOND SPEED TEST. Only fast proxies survive."""
    try:
        r = requests.get("https://cems.cpcb.gov.in/public/", proxies={"http": proxy_url, "https": proxy_url}, timeout=6, verify=False)
        if r.status_code == 200:
            return proxy_url
    except:
        pass
    return None

def get_verified_proxies(max_needed=10):
    candidates = fetch_indian_proxies()
    print(f"Testing {len(candidates)} candidate proxies against strict 6-second Speed Test...")
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=80) as executor:
        futures = {executor.submit(test_proxy_handshake, p): p for p in candidates}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                verified.append(res)
                print(f"Verified FAST Indian Gateway: {res}")
                if len(verified) >= max_needed:
                    break
    return verified

def block_heavy_assets(route):
    # Allow images so the screenshot works, but block video/audio
    if route.request.resource_type in ["media"]:
        route.abort()
    else:
        route.continue_()

def run_inspection():
    proxies = get_verified_proxies(max_needed=10)
    if not proxies:
        print("No fast proxies found, attempting direct connection...")
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
                print(f"Browser launch failed. Error: {e}")
                continue

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = context.new_page()
            page.set_default_timeout(180000)
            page.route("**/*", block_heavy_assets)

            captured_api_data = []
            
            def intercept_api_responses(response):
                if response.request.resource_type in ["xhr", "fetch"]:
                    try:
                        resp_text = response.text()
                        if "{" in resp_text and "}" in resp_text:
                            captured_api_data.append(resp_text)
                    except:
                        pass
                        
            page.on("response", intercept_api_responses)

            try:
                print("Navigating to Golden Link over secure Chromium tunnel...")
                page.goto(DIRECT_URL, wait_until="domcontentloaded", timeout=120000)

                print("Waiting up to 45 seconds for data to arrive...")
                success = False
                for _ in range(15):
                    page.wait_for_timeout(3000)
                    combined_json = "\n".join(captured_api_data).upper()
                    
                    if "SIDDHI" in combined_json or "7257" in combined_json or "MG/M" in combined_json:
                        print("SUCCESS! Intercepted API data.")
                        success = True
                        break

                print("Triggering the Emission tab for the screenshot...")
                try:
                    # Give the UI time to physically render
                    page.wait_for_selector(".ant-card, [role='tablist'], table", timeout=45000)
                    page.evaluate("""() => {
                        const tabs = Array.from(document.querySelectorAll('button, a, div, span, [role="tab"]'));
                        const emissionTab = tabs.find(el => el.textContent.trim().toUpperCase().includes('EMISSION'));
                        if (emissionTab) { emissionTab.click(); }
                    }""")
                    print("Tab clicked! Waiting 10 seconds for visual rendering...")
                    page.wait_for_timeout(10000)
                except Exception as fallback_err:
                    print(f"UI rendering timeout: {fallback_err}")
                    pass

                # ---------------------------------------------------------
                # 📸 TAKE THE DASHBOARD SNAPSHOT
                # ---------------------------------------------------------
                print("Taking a snapshot of the dashboard...")
                screenshot_path = "dashboard_snapshot.png"
                try:
                    page.screenshot(path=screenshot_path, timeout=15000)
                    print("Screenshot captured successfully.")
                except Exception as e:
                    print(f"Screenshot capture failed: {e}")
                    screenshot_path = None

                print("Extracting parameters...")
                page_full_text = page.inner_text("body")
                master_data_pool = f"{page_full_text}\n" + "\n".join(captured_api_data)
                
                upper_body = master_data_pool.upper()
                alert_reasons = []

                routine_val = "NA"
                routine_time = "Unknown"

                if "SIDDHI" not in upper_body and "MG/M" not in upper_body:
                    raise Exception("Proxy loaded a blank page. Moving to next proxy.")

                # Number Logic (Fixes the random NA bug)
                emission_numbers = re.findall(r"[\"']?(\d+(?:\.\d+)?)[\"']?\s*[,:]?\s*[\"']?(?:mg/m|mg/nm|µg/m)[\"']?", master_data_pool, re.IGNORECASE)
                if not emission_numbers:
                    emission_numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)", page_full_text, re.IGNORECASE)

                if emission_numbers:
                    val = float(emission_numbers[0])
                    routine_val = f"{val} mg/m³" 
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Emission reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")
                else:
                    is_na_detected = any(pattern in upper_body for pattern in [
                        " NA\n", " NA ", "\nNA\n", "N/A", "N.A", "DATA NOT AVAILABLE", '"NA"'
                    ])
                    if is_na_detected:
                        alert_reasons.append("• Reading is reported as <b>NA</b>")
                        routine_val = "NA (Data Not Available)"

                # Timestamp Extraction (Fixes the May 10th Date bug)
                now_ist = datetime.now(IST)
                timestamp_match = re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})|(\d{2}[/-]\d{2}[/-]\d{4}\s\d{2}:\d{2})", master_data_pool)

                if timestamp_match:
                    raw_ts_str = timestamp_match.group(0).strip()
                    try:
                        try:
                            parsed_dt = datetime.strptime(raw_ts_str, "%Y-%m-%d %H:%M")
                        except ValueError:
                            try:
                                parsed_dt = datetime.strptime(raw_ts_str.replace('/', '-'), "%d-%m-%Y %H:%M")
                            except ValueError:
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
                    print("Timestamp string not detected.")
                    if not success:
                        raise Exception("Proxy dropped connection mid-stream. Retrying.")

                # Scheduled Routine Check
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
                        f"🔗 <a href='{DIRECT_URL}'>Open CPCB Dashboard</a>"
                    )
                    
                    # Sends the message alongside the photo!
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
