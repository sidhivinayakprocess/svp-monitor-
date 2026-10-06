import os
import re
import json
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
        "https://www.proxy-list.download/api/v1/get?type=https&country=IN",
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
    if route.request.resource_type in ["media", "font"]:
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
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = context.new_page()
            page.set_default_timeout(180000)
            page.route("**/*", block_heavy_assets)

            captured_api_data = []
            
            # THE WIRETAP: Silently catches the JSON in the background while mimicking a human
            def intercept_api_responses(response):
                if response.request.resource_type in ["xhr", "fetch"]:
                    try:
                        resp_text = response.text()
                        if "{" in resp_text and "7257" in resp_text:
                            captured_api_data.append(resp_text)
                    except:
                        pass
                        
            page.on("response", intercept_api_responses)

            try:
                print("Navigating to Golden Link over secure Chromium tunnel (Bypassing WAF)...")
                # Wait for commit only, so we don't time out on slow DOM rendering
                page.goto(DIRECT_URL, wait_until="commit", timeout=120000)

                print("Waiting up to 45 seconds for API JSON data to arrive in background...")
                got_data = False
                for _ in range(15):
                    page.wait_for_timeout(3000)
                    combined_json = "\n".join(captured_api_data).upper()
                    
                    if "SIDDHI" in combined_json or "MG/M" in combined_json:
                        print("SUCCESS! Intercepted API data through the human-mimic tunnel.")
                        got_data = True
                        break

                if not got_data:
                    raise Exception("Failed to extract data payload. Moving to next proxy.")

                # ---------------------------------------------------------
                # SCREENSHOT LOGIC (WITH FAIL-SAFE)
                # ---------------------------------------------------------
                screenshot_path = "dashboard_snapshot.png"
                real_ui_success = False
                
                print("Attempting to render real CPCB Dashboard for screenshot...")
                try:
                    # Give it 30 seconds to render visually
                    page.wait_for_selector(".ant-card, [role='tablist'], table", timeout=30000)
                    page.evaluate("""() => {
                        const tabs = Array.from(document.querySelectorAll('button, a, div, span, [role="tab"]'));
                        const emissionTab = tabs.find(el => el.textContent.trim().toUpperCase().includes('EMISSION'));
                        if (emissionTab) { emissionTab.click(); }
                    }""")
                    page.wait_for_timeout(5000)
                    page.screenshot(path=screenshot_path, timeout=15000)
                    print("✅ Captured REAL CPCB Dashboard Screenshot.")
                    real_ui_success = True
                except Exception as fallback_err:
                    print(f"⚠️ Proxy too slow to render real UI ({fallback_err}). Triggering Fail-Safe local render...")
                    pass

                # ---------------------------------------------------------
                # DATA EXTRACTION FROM INTERCEPTED PAYLOAD
                # ---------------------------------------------------------
                print("Extracting precise parameters...")
                master_data_pool = "\n".join(captured_api_data).upper()
                
                alert_reasons = []
                routine_val = "NA"
                routine_time = "Unknown"

                # Extract mg/m3
                emission_numbers = re.findall(r"(\d+(?:\.\d+)?).{0,30}?(?:MG/M|MG/NM|µG/M)", master_data_pool)
                
                if emission_numbers:
                    val = float(emission_numbers[0])
                    routine_val = f"{val} mg/m³" 
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Emission reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")
                else:
                    if "NA" in master_data_pool or "NOT AVAILABLE" in master_data_pool:
                        alert_reasons.append("• Reading is reported as <b>NA</b>")
                        routine_val = "NA (Data Not Available)"

                # Extract Timestamp
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
                # EXECUTE FAIL-SAFE SCREENSHOT IF NEEDED
                # ---------------------------------------------------------
                is_alert = bool(alert_reasons)
                reasons_text = "\n".join(alert_reasons) if is_alert else ""
                
                if not real_ui_success:
                    print("Injecting captured data into Local Browser for Fallback Screenshot...")
                    status_color = "#d9363e" if (is_alert and 'below' in reasons_text) else "#52c41a"
                    delay_color = "#d9363e" if (is_alert and 'delayed' in reasons_text) else "#52c41a"
                    
                    html_content = f"""
                    <!DOCTYPE html><html><head><style>
                        body {{ font-family: Arial, sans-serif; background: #f0f2f5; padding: 40px; }}
                        .container {{ background: #fff; padding: 30px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.1); border-top: 6px solid #1890ff; max-width: 800px; margin: auto; }}
                        h1 {{ color: #1890ff; margin: 0 0 5px 0; }}
                        .grid {{ display: flex; gap: 20px; margin-top: 20px; text-align: center; }}
                        .box {{ flex: 1; background: #fafafa; padding: 20px; border: 1px solid #e8e8e8; border-radius: 8px; }}
                        .val {{ font-size: 32px; font-weight: bold; margin-top: 10px; }}
                    </style></head><body>
                        <div class="container">
                            <h1>🏭 SIDDHI VINAYAK PROCESS</h1>
                            <p style="color:#888; margin:0;">Live CEMS Intercept Dashboard (ID: 7257)</p>
                            <div class="grid">
                                <div class="box">
                                    <div style="color:#888; font-weight:bold;">CURRENT EMISSION</div>
                                    <div class="val" style="color: {status_color};">{routine_val}</div>
                                </div>
                                <div class="box">
                                    <div style="color:#888; font-weight:bold;">LAST RECEIVED</div>
                                    <div class="val" style="color: {delay_color};">{routine_time}</div>
                                </div>
                            </div>
                        </div>
                    </body></html>
                    """
                    page.set_content(html_content)
                    page.screenshot(path=screenshot_path)
                    print("✅ Captured Fallback Local Screenshot.")

                # ---------------------------------------------------------
                # NOTIFICATION LOGIC
                # ---------------------------------------------------------
                is_scheduled = (now_ist.hour == 11 and now_ist.minute < 30) or \
                               (now_ist.hour == 15 and now_ist.minute < 30) or \
                               (now_ist.hour == 21 and now_ist.minute >= 30)

                if alert_reasons or is_scheduled:
                    alert_time_str = now_ist.strftime("%I:%M %p (%d %b %Y)")
                    header = "⚠️ <b>CPCB Emission Alert</b>" if alert_reasons else "📊 <b>Scheduled Routine Update</b>"
                    
                    triggers_text = f"<b>Triggers:</b>\n{reasons_text}\n\n" if alert_reasons else ""
                        
                    telegram_msg = (
                        f"{header}\n\n"
                        f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                        f"<b>Current Reading:</b> {routine_val}\n"
                        f"<b>Last Received:</b> {routine_time}\n\n"
                        f"{triggers_text}"
                        f"🕒 <b>Report Time:</b> {alert_time_str}\n"
                        f"🔗 <a href='https://cems.cpcb.gov.in/#/public-dashboard'>Open CPCB Dashboard</a>"
                    )
                    
                    send_telegram(telegram_msg, image_path=screenshot_path)
                    print(f"Telegram notification sent! (Alert: {is_alert}, Scheduled: {is_scheduled})")
                else:
                    print("Status normal. All conditions within acceptable thresholds.")

                return # Exit the proxy loop on success

            except Exception as e:
                print(f"Iteration attempt failed on proxy {proxy}: {str(e)[:250]}")
            finally:
                browser.close()

    print("\nAll gateway proxies were exhausted during this cycle. The scheduler will retry automatically in 30 minutes.")

if __name__ == "__main__":
    run_inspection()
