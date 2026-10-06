import os
import re
import json
import concurrent.futures
from datetime import datetime
import requests
import pytz
from dateutil import parser
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
    """STRICT 6-SECOND SPEED TEST."""
    try:
        r = requests.get("https://cems.cpcb.gov.in/public/", proxies={"http": proxy_url, "https": proxy_url}, timeout=6, verify=False)
        if r.status_code == 200:
            return proxy_url
    except:
        pass
    return None

def fetch_data_via_proxy(proxy_url):
    """The Ultimate Speed Test: Sends the POST request directly."""
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

    is_scheduled = (now_ist.hour == 11 and now_ist.minute < 30) or \
                   (now_ist.hour == 15 and now_ist.minute < 30) or \
                   (now_ist.hour == 21 and now_ist.minute >= 30)

    if alert_reasons or is_scheduled:
        alert_time_str = now_ist.strftime("%I:%M %p (%d %b %Y)")
        header = "⚠️ <b>CPCB Emission Alert</b>" if alert_reasons else "📊 <b>Scheduled Routine Update</b>"
        
        triggers_text = f"<b>Triggers:</b>\n{reasons_text}\n\n" if alert_reasons else ""
        
        # Format the message nicely since we don't have a screenshot
        telegram_msg = (
            f"{header}\n\n"
            f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
            f"🟢 <b>Current Reading:</b> <code>{routine_val}</code>\n"
            f"🕒 <b>Last Received:</b> <code>{routine_time}</code>\n\n"
            f"{triggers_text}"
            f"📅 <b>Report Time:</b> {alert_time_str}\n"
            f"🔗 <a href='https://cems.cpcb.gov.in/#/public-dashboard'>Open CPCB Dashboard</a>"
        )
        
        send_telegram(telegram_msg)
        print(f"Telegram notification sent! (Alert: {is_alert}, Scheduled: {is_scheduled})")
    else:
        print("Status normal. All conditions within acceptable thresholds.")

if __name__ == "__main__":
    run_inspection()
