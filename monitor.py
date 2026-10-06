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

def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram keys missing.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
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
    # THE SECRET TO NO TIMEOUTS: 
    # By blocking images, media, fonts, AND stylesheets, the proxy only downloads ~10KB of text.
    # It bypasses the WAF perfectly but never crashes from bandwidth limits.
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
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
                "--ignore-certificate-errors", "--disable-http2", "--window-size=1920,1080"
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
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = context.new_page()
            page.set_default_timeout(180000)
            
            # Apply the bandwidth saver!
            page.route("**/*", block_heavy_assets)

            captured_api_data = []
            
            def intercept_api_responses(response):
                if response.request.resource_type in ["xhr", "fetch"]:
                    try:
                        resp_text = response.text()
                        if "{" in resp_text:
                            captured_api_data.append(resp_text)
                    except:
                        pass
                        
            page.on("response", intercept_api_responses)

            try:
                print("Navigating to Golden Link over secure Chromium tunnel...")
                # We only wait for 'commit' so we don't get stuck waiting for broken DOM elements
                page.goto(DIRECT_URL, wait_until="commit", timeout=120000)

                print("Waiting up to 45 seconds for data packets to arrive...")
                for _ in range(15):
                    page.wait_for_timeout(3000)
                    combined_json = "\n".join(captured_api_data).upper()
                    if "MG/M" in combined_json or "SIDDHI" in combined_json:
                        break

                print("Ensuring Emission tab is triggered...")
                try:
                    # Trigger the tab directly through the raw HTML DOM
                    page.evaluate("""() => {
                        const tabs = Array.from(document.querySelectorAll('button, a, div, span, [role="tab"], li'));
                        const emissionTab = tabs.find(el => el.textContent.trim().toUpperCase().includes('EMISSION'));
                        if (emissionTab) { emissionTab.click(); }
                    }""")
                    page.wait_for_timeout(8000) # Give numbers time to populate
                except:
                    pass

                print("Extracting parameters...")
                page_full_text = page.inner_text("body")
                master_data_pool = f"{page_full_text}\n" + "\n".join(captured_api_data)
                
                upper_body = master_data_pool.upper()
                alert_reasons = []
                routine_val = "NA"
                routine_time = "Unknown"

                # ---------------------------------------------------------
                # DATA EXTRACTION LOGIC
                # ---------------------------------------------------------
                # 1. Look strictly for numbers preceding mg/m3
                emission_numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:MG/M|MG/NM|µG/M)", upper_body)
                
                is_na_detected = any(pattern in upper_body for pattern in [
                    " NA\n", " NA ", "\nNA\n", "N/A", "N.A", "DATA NOT AVAILABLE", '"NA"'
                ])

                if emission_numbers:
                    val = float(emission_numbers[0])
                    routine_val = f"{val} mg/m³" 
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Emission reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")
                elif is_na_detected:
                    alert_reasons.append("• Reading is reported as <b>NA</b>")
                    routine_val = "NA (Data Not Available)"
                else:
                    raise Exception("Proxy loaded page, but no numerical data or 'NA' was found. Moving to next proxy.")

                # 2. Extract Timestamp
                now_ist = datetime.now(IST)
                timestamp_match = re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})|(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})|(\d{2}[/-]\d{2}[/-]\d{4}\s\d{2}:\d{2})", master_data_pool)

                if timestamp_match:
                    raw_ts_str = timestamp_match.group(0).strip()
                    try:
                        try:
                            parsed_dt = datetime.strptime(raw_ts_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
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

                # ---------------------------------------------------------
                # NOTIFICATION LOGIC
                # ---------------------------------------------------------
                is_scheduled = (now_ist.hour == 11 and now_ist.minute < 30) or \
                               (now_ist.hour == 15 and now_ist.minute < 30) or \
                               (now_ist.hour == 21 and now_ist.minute >= 30)

                if alert_reasons or is_scheduled:
                    alert_time_str = now_ist.strftime("%I:%M %p (%d %b %Y)")
                    header = "⚠️ <b>CPCB Emission Alert</b>" if alert_reasons else "📊 <b>Scheduled Routine Update</b>"
                    
                    triggers_text = f"<b>Triggers:</b>\n" + "\n".join(alert_reasons) + "\n\n" if alert_reasons else ""
                        
                    telegram_msg = (
                        f"{header}\n\n"
                        f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                        f"🟢 <b>Current Reading:</b> <code>{routine_val}</code>\n"
                        f"🕒 <b>Last Received:</b> <code>{routine_time}</code>\n\n"
                        f"{triggers_text}"
                        f"📅 <b>Report Time:</b> {alert_time_str}\n"
                        f"🔗 <a href='{DIRECT_URL}'>Open CPCB Dashboard</a>"
                    )
                    
                    send_telegram(telegram_msg)
                    print(f"Telegram notification sent! (Alert: {bool(alert_reasons)}, Scheduled: {is_scheduled})")
                else:
                    print("Status normal. All conditions within acceptable thresholds.")

                return # Exit loop completely upon success!

            except Exception as e:
                print(f"Iteration attempt failed on proxy {proxy}: {str(e)[:250]}")
            finally:
                browser.close()

    print("\nAll gateway proxies were exhausted during this cycle. The scheduler will retry automatically in 30 minutes.")

if __name__ == "__main__":
    run_inspection()
