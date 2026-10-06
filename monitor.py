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
# DIRECT CPCB API ENDPOINT & HEADERS (From known working cURL)
# ---------------------------------------------------------
API_URL = "https://cems.cpcb.gov.in/PUBLIC-DASHBOARD/v1/get_parameter_report"
PAYLOAD = {
    "data": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpbmR1c3RyeV9pZCI6ImluZHVzdHJ5XzcyNTcifQ.0XdJ1rrvb82-IiAigOLmCuitSnlNExMIxXi1CVKOqF8"
}

# These exact headers bypass the CPCB API firewall blocks
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://cems.cpcb.gov.in",
    "Referer": "https://cems.cpcb.gov.in/public/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
    """STRICT 6-SECOND SPEED TEST to weed out dead proxies."""
    try:
        r = requests.get("https://cems.cpcb.gov.in/public/", proxies={"http": proxy_url, "https": proxy_url}, timeout=6, verify=False)
        if r.status_code == 200:
            return proxy_url
    except:
        pass
    return None

def get_verified_proxies(max_needed=12):
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

def fetch_data_via_proxy(proxy_url):
    """Fires a direct 2KB API request bypassing the visual frontend."""
    try:
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        res = requests.post(API_URL, json=PAYLOAD, headers=HEADERS, proxies=proxies, timeout=15, verify=False)
        
        # Check if response is JSON and actually contains our data format
        if res.status_code == 200:
            try:
                data = res.json()
                # Ensure the JSON isn't an empty error response
                if data and isinstance(data, (dict, list)):
                    return proxy_url, data
            except json.JSONDecodeError:
                pass
        else:
            print(f"Proxy {proxy_url} connected but returned HTTP {res.status_code}")
    except Exception as e:
        print(f"Proxy {proxy_url} API Tunnel error: {type(e).__name__}")
    return proxy_url, None

def run_inspection():
    # 1. Get ONLY fast, verified proxies
    verified_proxies = get_verified_proxies(max_needed=12)
    if not verified_proxies:
        print("No responsive proxies found; attempting direct connection anyway.")
        verified_proxies = [None]
        
    print(f"\nDeploying API Swarm across {len(verified_proxies)} verified proxies...")
    api_data = None
    successful_proxy = None
    
    # 2. THE SWARM: Launch the POST request ONLY across the verified survivors
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_data_via_proxy, p): p for p in verified_proxies}
        for future in concurrent.futures.as_completed(futures):
            proxy, data = future.result()
            if data:
                api_data = data
                successful_proxy = proxy
                print(f"\nSUCCESS! Raw JSON Database payload retrieved instantly via: {successful_proxy}")
                executor.shutdown(wait=False, cancel_futures=True)
                break
                
    if not api_data:
        print("\nAll verified proxies failed to tunnel the POST request and retrieve valid JSON. Retrying next cycle.")
        return

    print("Extracting exact numerical parameters from raw JSON dictionary...")
    master_data_pool = json.dumps(api_data).upper()
    
    alert_reasons = []
    routine_val = "NA"
    routine_time = "Unknown"

    # 3. Precise Data Extraction
    # Look for a number immediately followed by or near the emission units
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

    # Strict Indian Date extraction
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

    # 4. Scheduled Routine Triggers (11 AM, 3 PM, 9:30 PM)
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
            f"🔗 <a href='https://cems.cpcb.gov.in/#/public-dashboard'>Open CPCB Dashboard</a>"
        )
        
        send_telegram(telegram_msg)
        print(f"Telegram notification sent! (Alert: {bool(alert_reasons)}, Scheduled: {is_scheduled})")
    else:
        print("Status normal. All conditions within acceptable thresholds.")

if __name__ == "__main__":
    run_inspection()
