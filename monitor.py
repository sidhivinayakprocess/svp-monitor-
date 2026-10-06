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
    """STRICT 6-SECOND SPEED TEST to weed out dead proxies."""
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
    # CRITICAL: We block ALL heavy visuals (images, css, fonts, videos) during the wiretap phase.
    # This prevents the free proxies from timing out while allowing the background JSON to load.
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        route.abort()
    else:
        route.continue_()

def generate_local_dashboard_and_screenshot(routine_val, routine_time, raw_json_text, is_alert, reasons_text, output_path="dashboard_snapshot.png"):
    """Generates a beautiful local HTML dashboard and snaps a picture instantly using a NON-PROXIED browser."""
    status_color = "#d9363e" if (is_alert and 'below' in reasons_text) else "#52c41a"
    delay_color = "#d9363e" if (is_alert and 'delayed' in reasons_text) else "#52c41a"
    
    # Try to make the JSON look pretty, fallback to raw text if it fails
    try:
        json_obj = json.loads(raw_json_text)
        pretty_json = json.dumps(json_obj, indent=4)
    except:
        pretty_json = raw_json_text

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f0f2f5; color: #333; padding: 40px; margin: 0; }}
            .container {{ background: #fff; border-radius: 12px; padding: 30px; box-shadow: 0 8px 24px rgba(0,0,0,0.1); max-width: 900px; margin: auto; border-top: 6px solid #1890ff; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #f0f0f0; padding-bottom: 20px; margin-bottom: 20px; }}
            .header h1 {{ margin: 0; color: #1890ff; font-size: 28px; }}
            .meta {{ color: #888; font-size: 14px; font-weight: 500; }}
            .widget-grid {{ display: flex; gap: 20px; margin-bottom: 30px; }}
            .widget {{ flex: 1; background: #fafafa; border: 1px solid #e8e8e8; border-radius: 8px; padding: 20px; text-align: center; }}
            .widget-title {{ font-size: 14px; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }}
            .widget-value {{ font-size: 36px; font-weight: bold; }}
            .raw-data {{ background: #282c34; padding: 20px; border-radius: 8px; font-family: 'Courier New', Courier, monospace; font-size: 14px; color: #abb2bf; white-space: pre-wrap; word-wrap: break-word; }}
            .footer {{ margin-top: 20px; font-size: 12px; color: #aaa; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🏭 SIDDHI VINAYAK PROCESS</h1>
                <div class="meta">Live CEMS Telemetry Dashboard • ID: 7257</div>
            </div>
            <div class="widget-grid">
                <div class="widget">
                    <div class="widget-title">Current Emission</div>
                    <div class="widget-value" style="color: {status_color};">{routine_val}</div>
                </div>
                <div class="widget">
                    <div class="widget-title">Last Received</div>
                    <div class="widget-value" style="color: {delay_color}; font-size: 24px; padding-top: 10px;">{routine_time}</div>
                </div>
            </div>
            <div class="widget-title" style="text-align: left;">Raw Database Payload intercepted from CPCB Network</div>
            <div class="raw-data">{pretty_json}</div>
            <div class="footer">Generated offline via secure data wiretap to prevent proxy timeouts</div>
        </div>
    </body>
    </html>
    """
    
    html_path = os.path.abspath("local_dashboard.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    print("Launching OFFLINE Playwright renderer for lightning-fast screenshot...")
    try:
        # NO PROXY USED HERE! This executes locally on the GitHub server.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(set_viewport_size={"width": 1000, "height": 850})
            page.goto(f"file://{html_path}")
            page.screenshot(path=output_path, full_page=True)
            browser.close()
        print("✅ Local screenshot captured successfully.")
        return output_path
    except Exception as e:
        print(f"Failed to render local screenshot: {e}")
        return None

def run_inspection():
    proxies = get_verified_proxies(max_needed=10)
    if not proxies:
        print("No fast proxies found, attempting direct connection...")
        proxies = [None]

    master_data_pool = None

    # =========================================================================
    # STAGE 1: THE WIRETAP (Using Proxies to bypass WAF and steal JSON)
    # =========================================================================
    for attempt, proxy in enumerate(proxies, 1):
        print(f"\n==================================================")
        print(f"STAGE 1 (WIRETAP) ATTEMPT {attempt}/{len(proxies)} -> Proxy: {proxy}")
        print(f"==================================================")

        with sync_playwright() as p:
            launch_args = [
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--ignore-certificate-errors", "--disable-http2"
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
            page.set_default_timeout(120000)
            
            # Massive bandwidth saver for the proxy
            page.route("**/*", block_heavy_assets)

            captured_api_data = []
            
            # THE BUG FIX: Capture all JSON. Do NOT filter by "7257" because the response body doesn't contain the ID!
            def intercept_api_responses(response):
                if response.request.resource_type in ["xhr", "fetch"]:
                    try:
                        text = response.text()
                        if "{" in text:
                            captured_api_data.append(text)
                    except:
                        pass
                        
            page.on("response", intercept_api_responses)

            try:
                print("Navigating to Golden Link over secure Chromium tunnel (Bypassing WAF)...")
                page.goto(DIRECT_URL, wait_until="commit", timeout=120000)

                print("Waiting up to 45 seconds for API JSON data containing 'MG/M3' to arrive...")
                got_data = False
                for _ in range(15):
                    page.wait_for_timeout(3000)
                    combined_json = "\n".join(captured_api_data).upper()
                    
                    if "MG/M" in combined_json or "MG/NM" in combined_json or "µG/M" in combined_json:
                        print("✅ SUCCESS! Intercepted API numerical data through the human-mimic tunnel.")
                        got_data = True
                        master_data_pool = combined_json
                        break

                if not got_data:
                    raise Exception("Failed to find numerical data payload. Moving to next proxy.")
                
                # Close the browser immediately to drop the proxy connection!
                browser.close()
                break # Exit the proxy loop! We have the data!

            except Exception as e:
                print(f"Iteration attempt failed on proxy {proxy}: {str(e)[:250]}")
            finally:
                if browser.is_connected():
                    browser.close()

    if not master_data_pool:
        print("\nAll gateway proxies were exhausted during this cycle. The scheduler will retry automatically in 30 minutes.")
        return

    # =========================================================================
    # STAGE 2: OFFLINE PROCESSING & SCREENSHOT
    # =========================================================================
    print("\nExtracting precise parameters from intercepted JSON...")
    
    alert_reasons = []
    routine_val = "NA"
    routine_time = "Unknown"

    # Extract numbers (e.g. captures 4.2 from "VALUE": 4.2, "UNIT": "MG/M3")
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

    is_alert = bool(alert_reasons)
    reasons_text = "\n".join(alert_reasons) if is_alert else ""

    # Launch Stage 2 Screenshot (Requires ZERO Proxy Bandwidth)
    screenshot_path = generate_local_dashboard_and_screenshot(routine_val, routine_time, master_data_pool, is_alert, reasons_text)

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
        print(f"Telegram notification sent WITH local snapshot! (Alert: {is_alert}, Scheduled: {is_scheduled})")
    else:
        print("Status normal. All conditions within acceptable thresholds.")

if __name__ == "__main__":
    run_inspection()
