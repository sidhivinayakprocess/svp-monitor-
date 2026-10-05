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

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
DASHBOARD_URL = "https://cems.cpcb.gov.in/public/#/l/realtime-connectivity-status-dashboard"
TARGET_INDUSTRY = "SIDDHI VINAYAK PROCESS"
THRESHOLD = 5.0
DELAY_THRESHOLD_HOURS = 1.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# TELEGRAM DISPATCHER
# ---------------------------------------------------------------------------
def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram keys missing from environment secrets.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=20)
        print(f"Telegram API response: {res.status_code}")
    except Exception as e:
        print(f"Telegram notification error: {e}")

# ---------------------------------------------------------------------------
# MULTI-SOURCE INDIAN PROXY SWARM
# ---------------------------------------------------------------------------
def fetch_candidate_proxies():
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=6000&country=IN&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http&country=IN",
        "https://www.proxy-list.download/api/v1/get?type=https&country=IN",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
        "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    ]
    proxies = set()
    print("Scraping active Indian proxies...")
    for src in sources:
        try:
            r = requests.get(src, timeout=8)
            if r.status_code == 200:
                for match in re.finditer(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b", r.text):
                    proxies.add(match.group(0))
        except Exception:
            continue
    return list(proxies)

def test_proxy_handshake(proxy_ip):
    p_url = f"http://{proxy_ip}"
    try:
        r = requests.get(
            "https://cems.cpcb.gov.in/public/",
            proxies={"http": p_url, "https": p_url},
            timeout=5,
            verify=False
        )
        if r.status_code in [200, 301, 302, 401, 403]:
            return p_url
    except Exception:
        pass
    return None

def get_verified_proxies(max_needed=5):
    candidates = fetch_candidate_proxies()
    print(f"Testing {len(candidates)} candidate proxies against CPCB gateway...")
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
        futures = {executor.submit(test_proxy_handshake, p): p for p in candidates}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                verified.append(res)
                print(f"Verified Indian Gateway: {res}")
                if len(verified) >= max_needed:
                    break
    return verified

# ---------------------------------------------------------------------------
# BANDWIDTH OPTIMIZER
# ---------------------------------------------------------------------------
def abort_heavy_assets(route):
    if route.request.resource_type in ["image", "media", "font"]:
        route.abort()
    else:
        route.continue_()

# ---------------------------------------------------------------------------
# CORE SCRAPING & EVALUATION ENGINE
# ---------------------------------------------------------------------------
def run_inspection():
    proxies = get_verified_proxies(max_needed=4)
    if not proxies:
        print("No responsive Indian proxies found; falling back to direct attempt.")
        proxies = [None]

    for attempt, proxy in enumerate(proxies, 1):
        print(f"\n==================================================")
        print(f"ATTEMPT {attempt}/{len(proxies)} -> Proxy: {proxy}")
        print(f"==================================================")

        with sync_playwright() as p:
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--window-size=1920,1080"
            ]
            launch_opts = {"headless": True, "args": launch_args}
            if proxy:
                launch_opts["proxy"] = {"server": proxy}

            browser = p.chromium.launch(**launch_opts)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )
            page = context.new_page()
            page.set_default_timeout(35000)
            page.route("**/*", abort_heavy_assets)

            try:
                # 1. Navigate to main dashboard
                print("Navigating to CPCB dashboard...")
                page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=75000)

                # 2. Wait for Ant Design table hydration
                print("Waiting for grid rows to render...")
                page.wait_for_selector("table, .ant-table, tr", timeout=45000)
                page.wait_for_timeout(6000)

                # 3. Locate writable search filter inputs
                print("Locating search filter inputs...")
                all_inputs = page.locator("input:not([readonly]):not([disabled])").all()
                visible_inputs = [inp for inp in all_inputs if inp.is_visible()]

                if not visible_inputs:
                    raise Exception("No active search inputs rendered on dashboard.")

                row_isolated = False
                for idx, field in enumerate(visible_inputs, 1):
                    print(f"Testing filter input #{idx}...")
                    try:
                        field.click(timeout=3000)
                        field.fill("")
                        field.type(TARGET_INDUSTRY, delay=40)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(4000)

                        # Check if target row is now isolated in the table
                        match_count = page.locator("tr, .ant-table-row", has_text=re.compile(TARGET_INDUSTRY, re.IGNORECASE)).count()
                        if match_count > 0:
                            print(f"Row successfully matched using filter input #{idx}!")
                            row_isolated = True
                            break
                        else:
                            field.fill("")
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(1000)
                    except Exception as err:
                        print(f"Field #{idx} skipped: {err}")

                if not row_isolated:
                    raise Exception("Failed to isolate industry row in table.")

                # Extract captured row data
                matched_row = page.locator("tr, .ant-table-row", has_text=re.compile(TARGET_INDUSTRY, re.IGNORECASE)).first
                row_raw_text = matched_row.inner_text()

                # 4. Click the Action Eye Icon via in-page JavaScript evaluation
                # This bypasses Ant Design's fixed-right floating table layout (.ant-table-fixed-right)
                print("Triggering Action Eye Icon via DOM dispatcher...")
                click_action_result = page.evaluate("""() => {
                    // Check for Ant Design eye SVG / icon classes
                    const eye = document.querySelector('.anticon-eye, [nztype="eye"], [data-icon="eye"], svg.ant-icon-eye');
                    if (eye) {
                        (eye.closest('button, a') || eye).click();
                        return 'Clicked via .anticon-eye';
                    }

                    // Check inside fixed-right column container
                    const fixedRight = document.querySelector('.ant-table-fixed-right');
                    if (fixedRight) {
                        const btn = fixedRight.querySelector('tbody tr a, tbody tr button, tbody tr i, tbody tr svg');
                        if (btn) {
                            btn.click();
                            return 'Clicked via .ant-table-fixed-right container';
                        }
                    }

                    // Fallback to last cell clickable element
                    const rows = Array.from(document.querySelectorAll('tbody tr')).filter(r => 
                        r.textContent.toUpperCase().includes('SIDDHI VINAYAK')
                    );
                    if (rows.length > 0) {
                        const cell = rows[0].querySelector('td:last-child');
                        if (cell) {
                            (cell.querySelector('button, a, i, svg') || cell).click();
                            return 'Clicked via last column cell';
                        }
                    }
                    return null;
                }""")

                if not click_action_result:
                    raise Exception("Action Eye icon could not be located or clicked.")
                print(f"Action button status: {click_action_result}")
                page.wait_for_timeout(5000)

                # 5. Select the Emission Button
                print("Switching to Emission tab/button...")
                emission_click_result = page.evaluate("""() => {
                    const nodes = Array.from(document.querySelectorAll('button, [role="tab"], .ant-tabs-tab, a, span, div'));
                    for (const el of nodes) {
                        const txt = el.textContent.trim().toUpperCase();
                        if (txt === 'EMISSION' && el.offsetWidth > 0) {
                            el.click();
                            return 'Clicked exact Emission tab';
                        }
                    }
                    for (const el of nodes) {
                        const txt = el.textContent.trim().toUpperCase();
                        if (txt.includes('EMISSION') && (el.tagName === 'BUTTON' || el.getAttribute('role') === 'tab' || el.classList.contains('ant-tabs-tab'))) {
                            el.click();
                            return 'Clicked fuzzy Emission tab';
                        }
                    }
                    return null;
                }""")
                print(f"Emission panel status: {emission_click_result}")
                page.wait_for_timeout(4000)

                # 6. Capture rendered readings from modal / card view
                page_full_text = page.inner_text("body")
                full_combined_text = f"{row_raw_text}\n{page_full_text}"

                print("---- PREVIEW OF EXTRACTED DATA ----")
                print("\n".join([line for line in full_combined_text.splitlines() if line.strip()][:25]))
                print("-----------------------------------")

                # ---------------------------------------------------------
                # 7. EVALUATE THE 3 ALERT CONDITIONS
                # ---------------------------------------------------------
                alert_reasons = []

                # Condition 1: Check for NA / Data Not Available
                upper_body = full_combined_text.upper()
                is_na_detected = any(pattern in upper_body for pattern in [
                    " NA\n", " NA ", "\nNA\n", "N/A", "N.A", "DATA NOT AVAILABLE", "NOT AVAILABLE"
                ])
                if is_na_detected:
                    alert_reasons.append("• Reading is reported as <b>NA</b>")

                # Condition 2: Numeric reading < 5.0 mg/m3
                # Match numbers preceded or followed by emission units (mg/m3, mg/Nm3, etc.)
                emission_numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg/m|mg/nm|µg/m)?", page_full_text, re.IGNORECASE)
                if emission_numbers and not is_na_detected:
                    val = float(emission_numbers[0])
                    if val < THRESHOLD:
                        alert_reasons.append(f"• Emission value (<b>{val} mg/m³</b>) is below {THRESHOLD} mg/m³")
                    else:
                        print(f"Emission reading normal: {val} mg/m³ >= {THRESHOLD} mg/m³")

                # Condition 3: "Last received:" timestamp delayed by >= 1 hour
                # Patterns like '2026-10-05 14:15' or '05-10-2026 14:15' or labeled 'Last received: ...'
                now_ist = datetime.now(IST)
                timestamp_match = re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})|(\d{2}[/-]\d{2}[/-]\d{4}\s\d{2}:\d{2})", full_combined_text)

                if timestamp_match:
                    raw_ts_str = timestamp_match.group(0).strip()
                    try:
                        parsed_dt = parser.parse(raw_ts_str, fuzzy=True, dayfirst=True)
                        last_received_ist = IST.localize(parsed_dt) if parsed_dt.tzinfo is None else parsed_dt.astimezone(IST)

                        time_diff = now_ist - last_received_ist
                        delay_hours = time_diff.total_seconds() / 3600.0

                        print(f"Current IST: {now_ist.strftime('%Y-%m-%d %H:%M')}")
                        print(f"Last Received: {last_received_ist.strftime('%Y-%m-%d %H:%M')} (Delay: {delay_hours:.2f} hrs)")

                        if delay_hours >= DELAY_THRESHOLD_HOURS:
                            h = int(time_diff.total_seconds() // 3600)
                            m = int((time_diff.total_seconds() % 3600) // 60)
                            alert_reasons.append(f"• Telemetry delayed by <b>{h}h {m}m</b> (Last received: {last_received_ist.strftime('%d %b %I:%M %p')})")
                    except Exception as parse_err:
                        print(f"Timestamp parsing error for '{raw_ts_str}': {parse_err}")
                else:
                    print("Timestamp string not detected in extracted text.")

                # 8. Dispatch notification if any conditions met
                if alert_reasons:
                    reasons_text = "\n".join(alert_reasons)
                    alert_time_str = now_ist.strftime("%I:%M %p (%d %b %Y)")
                    telegram_msg = (
                        f"⚠️ <b>CPCB Emission Alert</b>\n\n"
                        f"🏭 <b>Industry:</b> {TARGET_INDUSTRY}\n\n"
                        f"<b>Triggers:</b>\n{reasons_text}\n\n"
                        f"🕒 <b>Alert Time:</b> {alert_time_str}\n"
                        f"🔗 <a href='{DASHBOARD_URL}'>Open CPCB Dashboard</a>"
                    )
                    send_telegram(telegram_msg)
                    print(f"ALERT TRIGGERED! Telegram notification sent.")
                else:
                    print("Status normal. All conditions within acceptable thresholds.")

                # Successful execution completed
                return

            except Exception as e:
                print(f"Iteration attempt failed on proxy {proxy}: {str(e)[:250]}")
            finally:
                browser.close()

    print("\nAll gateway proxies were exhausted during this cycle. The scheduler will retry automatically in 30 minutes.")

if __name__ == "__main__":
    run_inspection()
