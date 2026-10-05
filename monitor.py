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
        "https://www.proxy-list.download/api/v1/get?type=https&country=IN"
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

def get_verified_proxies(max_needed=4):
    candidates = fetch_indian_proxies()
    print(f"Testing {len(candidates)} candidate proxies against CPCB firewall...")
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
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
    # Cuts data weight by 70%, crucial for slow free proxies
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        route.abort()
    else:
        route.continue_()

def run_inspection():
    proxies = get_verified_proxies(max_needed=4)
    if not proxies:
        print("No responsive proxies found; falling back to direct attempt.")
        proxies = [None]

    for attempt, proxy in enumerate(proxies, 1):
        print(f"\n==================================================")
        print(f"ATTEMPT {attempt}/{len(proxies)} -> Proxy: {proxy}")
        print(f"==================================================")

        with sync_playwright() as p:
            # USING FIREFOX to stop the proxy connection drops!
            launch_opts = {"headless": True}
            if proxy:
                launch_opts["proxy"] = {"server": proxy}

            browser = p.firefox.launch(**launch_opts)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = context.new_page()
            
            # Massive 3-Minute Timeout
            page.set_default_timeout(180000)
            page.route("**/*", block_heavy_assets)

            # ---------------------------------------------------------
            # 1. API NETWORK INTERCEPTOR (WIRETAP)
            # ---------------------------------------------------------
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
                print("Navigating to Golden Link over secure Firefox tunnel...")
                # commit wait prevents waiting for heavy frontend assets
                page.goto(DIRECT_URL, wait_until="commit", timeout=180000)

                print("Waiting up to 90 seconds for API data packets to arrive...")
                for _ in range(30):
                    page.wait_for_timeout(3000)
                    combined_json = "\n".join(captured_api_data).upper()
                    
                    if "SIDDHI" in combined_json or "7257" in combined_json or "MG/M" in combined_json:
                        print("SUCCESS! Intercepted raw JSON API payload from network background.")
                        break

                print("Ensuring Emission tab is triggered...")
                try:
                    page.evaluate("""() => {
                        const tabs = Array.from(document.querySelectorAll('button, a, div, span, [role="tab"]'));
                        const emissionTab = tabs.find(el => el.textContent.trim().toUpperCase().includes('EMISSION'));
                        if (emissionTab) { emissionTab.click(); }
                    }""")
                    page.wait_for_timeout(5000)
                except:
                    pass

                # ---------------------------------------------------------
                # 2. DATA EXTRACTION & ANALYSIS
                # ---------------------------------------------------------
                print("Extracting parameters...")
                
                page_full_text = page.inner_text("body")
                master_data_pool = f"{page_full_text}\n" + "\n".join(captured_api_data)
                
                upper_body = master_data_pool.upper()
                alert_reasons = []

                # Condition 1: Check for NA
                is_na_detected = any(pattern in upper_body for pattern in [
                    " NA\n", " NA ", "\nNA\n", "N/A", "N.A", "DATA NOT AVAILABLE", '"NA"'
                ])
                if is_na_detected:
                    alert_reasons.append("• Reading is reported as <b>NA</b>")

                # Condition 2: Numeric reading < 5.0 mg/m3
                emission_numbers = re.findall(r"[\"']?(\d+(?:\.\d+)?)[\"']?\s*[,:]?\s*[\"']?(?:mg/m|mg/nm|µg/m)[\"']?", master_data_pool, re.IGNORECASE)
                
                if not emission_numbers:
                    emission_numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)", page_full_text, re.IGNORECASE)

                if emission_numbers and not is_na_detected:
                    val = float(emission_numbers[0])
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Emission reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")

                # ---------------------------------------------------------
                # Condition 3: "Last received:" timestamp (MAY 10TH BUG FIXED)
                # ---------------------------------------------------------
                now_ist = datetime.now(IST)
                timestamp_match = re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})|(\d{2}[/-]\d{2}[/-]\d{4}\s\d{2}:\d{2})", master_data_pool)

                if timestamp_match:
                    raw_ts_str = timestamp_match.group(0).strip()
                    try:
                        # Try strict YYYY-MM-DD parsing first
                        try:
                            parsed_dt = datetime.strptime(raw_ts_str, "%Y-%m-%d %H:%M")
                        except ValueError:
                            # Try DD-MM-YYYY fallback
                            try:
                                parsed_dt = datetime.strptime(raw_ts_str.replace('/', '-'), "%d-%m-%Y %H:%M")
                            except ValueError:
                                # Safe library fallback
                                parsed_dt = parser.parse(raw_ts_str)

                        last_received_ist = IST.localize(parsed_dt) if parsed_dt.tzinfo is None else parsed_dt.astimezone(IST)
                        time_diff = now_ist - last_received_ist
                        delay_hours = time_diff.total_seconds() / 3600.0

                        print(f"Current IST: {now_ist.strftime('%Y-%m-%d %H:%M')}")
                        print(f"Last Received: {last_received_ist.strftime('%Y-%m-%d %H:%M')} (Delay: {delay_hours:.2f} hrs)")

                        if delay_hours >= DELAY_THRESHOLD_HOURS:
                            h = int(time_diff.total_seconds() // 3600)
                            m = int((time_diff.total_seconds() % 3600) // 60)
                            alert_reasons.append(f"• Telemetry delayed by <b>{h}h {m}m</b> (Last received: {last_received_ist.strftime('%d %b %Y, %I:%M %p')})")
                    except Exception as parse_err:
                        print(f"Timestamp parsing error: {parse_err}")
                else:
                    print("Timestamp string not detected in extracted data pool.")

                # ---------------------------------------------------------
                # 3. DISPATCH NOTIFICATION
                # ---------------------------------------------------------
                if alert_reasons:
                    reasons_text = "\n".join(alert_reasons)
                    alert_time_str = now_ist.strftime("%I:%M %p (%d %b %Y)")
                    telegram_msg = (
                        f"⚠️ <b>CPCB Emission Alert</b>\n\n"
                        f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                        f"<b>Triggers:</b>\n{reasons_text}\n\n"
                        f"🕒 <b>Alert Time:</b> {alert_time_str}\n"
                        f"🔗 <a href='{DIRECT_URL}'>Open CPCB Dashboard</a>"
                    )
                    send_telegram(telegram_msg)
                    print(f"ALERT TRIGGERED! Telegram notification sent.")
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
