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

TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0
DELAY_THRESHOLD_HOURS = 1.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------
# DIRECT CPCB API ENDPOINT & EXACT HEADERS
# ---------------------------------------------------------
API_URL = "https://cems.cpcb.gov.in/PUBLIC-DASHBOARD/v1/get_parameter_report"
PAYLOAD = {
    "data": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpbmR1c3RyeV9pZCI6ImluZHVzdHJ5XzcyNTcifQ.0XdJ1rrvb82-IiAigOLmCuitSnlNExMIxXi1CVKOqF8"
}

# Utilizing exact headers from the successful curl bypass test
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Origin": "https://cems.cpcb.gov.in",
    "Referer": "https://cems.cpcb.gov.in/public/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"'
}

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

def fetch_data_via_proxy(proxy_url):
    """The Ultimate Speed Test: Sends the POST request directly. First to succeed wins."""
    try:
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        res = requests.post(API_URL, data=json.dumps(PAYLOAD), headers=HEADERS, proxies=proxies, timeout=10, verify=False)
        
        if res.status_code == 200:
            try:
                data = res.json()
                if data and isinstance(data, (dict, list)):
                    return proxy_url, data
            except json.JSONDecodeError:
                pass
    except:
        pass
    return proxy_url, None

def generate_local_dashboard_and_screenshot(routine_val, routine_time, api_data, is_alert, reasons_text, output_path="dashboard_snapshot.png"):
    """Generates a beautiful local HTML dashboard and snaps a picture instantly using Playwright."""
    status_color = "#d9363e" if (is_alert and 'below' in reasons_text) else "#52c41a"
    delay_color = "#d9363e" if (is_alert and 'delayed' in reasons_text) else "#52c41a"

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
            <div class="widget-title" style="text-align: left;">Raw Database Payload</div>
            <div class="raw-data">{json.dumps(api_data, indent=4)}</div>
            <div class="footer">Generated locally via direct API Tunnel • Bypassed Web UI rendering</div>
        </div>
    </body>
    </html>
    """
    
    html_path = os.path.abspath("local_dashboard.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    print("Launching local Playwright renderer for screenshot...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(set_viewport_size={"width": 1000, "height": 850})
            page.goto(f"file://{html_path}")
            page.screenshot(path=output_path, full_page=True)
            browser.close()
        print("Local screenshot captured successfully.")
        return output_path
    except Exception as e:
        print(f"Failed to render local screenshot: {e}")
        return None

def run_inspection():
    candidates = fetch_indian_proxies()
    print(f"Found {len(candidates)} candidate proxies. Deploying API Swarm (testing 60 concurrently)...")
    
    api_data = None
    successful_proxy = None
    
    # Unleash the Swarm: The absolute fastest proxy to fetch the data wins.
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
        futures = {executor.submit(fetch_data_via_proxy, f"http://{p}"): p for p in candidates}
        for future in concurrent.futures.as_completed(futures):
            proxy, data = future.result()
            if data:
                api_data = data
                successful_proxy = proxy
                print(f"\nSUCCESS! Raw JSON Database payload retrieved instantly via: {successful_proxy}")
                
                # Cancel remaining requests to save memory
                for f in futures:
                    f.cancel()
                break
                
    if not api_data:
        print("\nAll proxies failed to tunnel the POST request. Retrying next cycle.")
        return

    print("Extracting exact numerical parameters from raw JSON dictionary...")
    master_data_pool = json.dumps(api_data).upper()
    
    alert_reasons = []
    routine_val = "NA"
    routine_time = "Unknown"

    emission_numbers = re.findall(r"(\d+(?:\.\d+)?)[^A-Za-z]{0,10}(?:MG/M|MG/NM|µG/M)", master_data_pool)
    
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
        print("Timestamp string not detected in raw API response.")

    is_alert = bool(alert_reasons)
    reasons_text = "\n".join(alert_reasons) if is_alert else ""

    # Generate the local dashboard and take a screenshot offline
    screenshot_path = generate_local_dashboard_and_screenshot(routine_val, routine_time, api_data, is_alert, reasons_text)

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
