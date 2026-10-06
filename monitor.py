import concurrent.futures
import os
import random
import re
import threading
import time
from datetime import datetime

import pytz
import requests
import urllib3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

urllib3.disable_warnings()

IST = pytz.timezone("Asia/Kolkata")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CEMS_PROXY = os.environ.get("CEMS_PROXY")   # optional; else free Indian proxies
INDUSTRY = "SIDDHI VINAYAK PROCESS"
INDUSTRY_RX = re.compile(r"SIDDHI\s+VINAYAK", re.I)
DASH_URL = ("https://cems.cpcb.gov.in/public/#/"
            "l/realtime-connectivity-status-dashboard")
DELAY_LIMIT_HOURS = 1.0
NAV_TIMEOUT_MS = 45000
PER_ATTEMPT_S = 150
MAX_ATTEMPTS = 5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

FREE_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http"
    "&timeout=5000&country=IN&ssl=all&anonymity=all",
    "https://www.proxy-list.download/api/v1/get?type=http&country=IN",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-Lists/master/http.txt",
]
GEONODE_URL = ("https://proxylist.geonode.com/api/proxy-list?limit=500&page=1"
               "&sort_by=lastChecked&sort_type=desc&country=IN"
               "&protocols=http%2Chttps")

# ------------------------------------------------------------ telegram ------
def tg(method, **kwargs):
    if not BOT_TOKEN or not CHAT_ID:
        print("!! Telegram creds missing — skipping notify")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                          timeout=30, **kwargs)
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            print(f"!! Telegram {method}: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"!! Telegram error: {e}")
        return False

def send_text(m):
    return tg("sendMessage", json={"chat_id": CHAT_ID, "text": m[:4096],
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": True})

def send_photo(path, caption=""):
    if not path or not os.path.exists(path):
        return send_text(caption)
    try:
        with open(path, "rb") as fh:
            return tg("sendPhoto", data={"chat_id": CHAT_ID,
                                         "caption": caption[:1024],
                                         "parse_mode": "HTML"},
                      files={"photo": fh})
    except Exception as e:
        print(f"!! sendPhoto: {e}")
        return send_text(caption)

# ------------------------------------------------------ human-like helpers --
def human_pause(a=0.8, b=2.2):
    time.sleep(random.uniform(a, b))

def human_move_click(page, locator):
    box = locator.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                        box["y"] + box["height"] * random.uniform(0.3, 0.7),
                        steps=random.randint(5, 15))
    human_pause(0.3, 0.9)
    locator.click()

def run_with_timeout(fn, timeout_s, *args):
    box = {}
    def target():
        try:
            box["val"] = fn(*args)
        except Exception as e:          # noqa: BLE001
            box["err"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"attempt exceeded {timeout_s}s")
    if "err" in box:
        raise box["err"]
    return box["val"]

# -------------------------------------------------------------- proxies -----
def fetch_candidates():
    """Return {ip: proxy_url} from all sources."""
    urls = set()
    for src in FREE_PROXY_SOURCES:
        try:
            r = requests.get(src, timeout=8)
            if r.status_code == 200:
                urls.update("http://" + m.group(0) for m in
                            re.finditer(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b",
                                        r.text))
        except Exception:
            pass
    try:
        r = requests.get(GEONODE_URL, timeout=10)
        if r.status_code == 200:
            for d in r.json().get("data", []):
                if d.get("ip") and d.get("port"):
                    urls.add(f"http://{d['ip']}:{d['port']}")
    except Exception:
        pass
    cands = {}
    for u in urls:
        ip = u.split("//")[1].split(":")[0]
        cands[ip] = u
    print(f"{len(cands)} candidate proxies")
    return cands

def indian_ips(ips):
    """Keep only India-geolocated IPs (ip-api.com free batch API, 100/req)."""
    keep = set()
    batches = [ips[i:i + 100] for i in range(0, min(len(ips), 600), 100)]
    for batch in batches:
        try:
            r = requests.post(
                "http://ip-api.com/batch?fields=query,status,countryCode",
                json=batch, timeout=12)
            if r.status_code == 200:
                for item in r.json():
                    if (item and item.get("status") == "success"
                            and item.get("countryCode") == "IN"):
                        keep.add(item.get("query"))
        except Exception:
            pass
    print(f"geolocation: {len(keep)}/{len(ips)} are IN")
    return keep

def check_proxy(p):
    try:
        t0 = time.time()
        r = requests.get("https://cems.cpcb.gov.in/public/",
                         proxies={"http": p, "https": p},
                         timeout=6, verify=False,
                         headers={"User-Agent": UA,
                                  "Accept": "text/html,application/xhtml+xml",
                                  "Accept-Language": "en-IN,en;q=0.9"})
        if r.status_code == 200 and (time.time() - t0) < 5.5:
            return (int((time.time() - t0) * 1000), p)
    except Exception:
        pass
    return None

def best_proxies():
    if CEMS_PROXY:
        return [CEMS_PROXY]
    cands = fetch_candidates()
    if not cands:
        return [None]
    indian = indian_ips(list(cands))
    shortlist = [cands[ip] for ip in indian] or list(cands.values())[:50]
    res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=80) as ex:
        for r in ex.map(check_proxy, shortlist):
            if r:
                res.append(r)
    res.sort()
    out = [p for _, p in res[:MAX_ATTEMPTS]]
    print(f"validated {len(res)}, using {out}")
    return out or [None]

# ------------------------------------------------------------- the flow -----
def attempt(proxy_server):
    with sync_playwright() as p:
        kw = dict(headless=False, args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1366,768", "--lang=en-IN",
            "--ignore-certificate-errors"])
        if proxy_server:
            kw["proxy"] = {"server": proxy_server}
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(
            user_agent=UA, locale="en-IN", timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
            geolocation={"latitude": 20.59, "longitude": 78.96},
            permissions=["geolocation"], ignore_https_errors=True,
            color_scheme="light")
        Stealth().apply_stealth_sync(ctx)
        page = ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        def snap(name):
            try:
                page.screenshot(path=f"{name}.png", full_page=True)
                open(f"{name}.html", "w", encoding="utf-8").write(page.content())
            except Exception as e:
                print(f"snap {name}: {e}")

        def page_diag():
            """Self-diagnosis string appended to any error."""
            try:
                return (f" | url={page.url} | title={page.title()!r} | "
                        f"body={page.locator('body').inner_text()[:250]!r}")
            except Exception:
                return " | (could not read page state)"

        try:
            page.goto(DASH_URL, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            human_pause(8.0, 12.0)              # let the SPA render
            snap("1_dashboard")

            # find the industry by visible text — no table-type assumption
            cell = page.get_by_text(INDUSTRY_RX).first
            cell.wait_for(timeout=25000)
            row = None
            for anc in ["xpath=ancestor::tr[1]",
                        "xpath=ancestor::*[@role='row'][1]",
                        "xpath=ancestor::div[contains(@class,'row')][1]"]:
                try:
                    loc = cell.locator(anc)
                    if loc.count():
                        row = loc.first
                        break
                except Exception:
                    continue
            if row is None:
                row = cell
            try:
                row.scroll_into_view_if_needed()
            except Exception:
                pass
            human_pause(1.0, 2.0)

            # eye / view icon inside the row (or the text itself)
            opened = False
            for sel in ["[aria-label*='iew' i]", "[title*='iew' i]",
                        "mat-icon:has-text('visibility')",
                        "button:has(mat-icon)", "a:has(mat-icon)",
                        "button:has(i.material-icons)", "button:has(svg)",
                        "button", "a"]:
                try:
                    btns = row.locator(sel)
                    for i in range(min(btns.count(), 6)):
                        b = btns.nth(i)
                        if b.is_visible():
                            human_move_click(page, b)
                            opened = True
                            break
                    if opened:
                        break
                except Exception:
                    continue
            if not opened:
                human_move_click(page, row)
            human_pause(3.0, 5.0)
            snap("2_site_info")

            # Emissions tab by visible text
            tab = page.get_by_text(re.compile(r"Emission", re.I)).first
            tab.wait_for(timeout=20000)
            human_pause(0.8, 1.8)
            human_move_click(page, tab)
            human_pause(5.0, 7.5)
            snap("3_emissions")

            rows = []
            tables = page.locator("table")
            for t in range(min(tables.count(), 6)):
                html = tables.nth(t).inner_html()
                if re.search(r"mg|µg|ug", html, re.I):
                    for tr in tables.nth(t).locator("tr").all():
                        cells = [c.strip() for c in
                                 tr.locator("th, td").all_inner_texts()]
                        if cells:
                            rows.append(cells)
            page_text = page.locator("body").inner_text()
            browser.close()
            return "data", (rows, page_text)
        except Exception as e:
            diag = page_diag()
            try:
                snap("error")
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            return "error", f"{type(e).__name__}: {e}{diag}"

def scrape(proxies):
    errors = []
    for i, prox in enumerate(proxies, 1):
        print(f"--- attempt {i}/{len(proxies)} via {prox or 'DIRECT'} ---")
        try:
            kind, payload = run_with_timeout(attempt, PER_ATTEMPT_S, prox)
        except TimeoutError:
            errors.append(f"attempt {i} ({prox or 'DIRECT'}): timed out "
                          f"after {PER_ATTEMPT_S}s")
            continue
        except Exception as e:
            errors.append(f"attempt {i} ({prox or 'DIRECT'}): {e}")
            continue
        if kind == "data":
            return payload, errors
        errors.append(f"attempt {i} ({prox or 'DIRECT'}): {payload}")
        print(f"failed: {payload[:200]}")
    return None, errors

# ------------------------------------------------------------- parsing ------
def is_na(v):
    return str(v).strip().lower() in ("", "na", "n/a", "null", "none",
                                      "nan", "-", "--", "not available")

def parse_dt(s):
    if not s:
        return None
    try:
        from dateutil import parser as dparser
        dt = dparser.parse(s.strip().replace("/", "-"), dayfirst=True)
        return IST.localize(dt) if dt.tzinfo is None else dt.astimezone(IST)
    except Exception:
        return None

def pick_reading(rows, page_text=""):
    best = None
    for cells in rows or []:
        if len(cells) < 3:
            continue
        value = unit = dt = None
        for c in cells:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(mg/m|µg/m|ug/m|mg/nm)", c, re.I)
            if m and value is None:
                value, unit = m.group(1), m.group(2)
            if re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}:\d{2}", c) and not dt:
                dt = parse_dt(c)
        if value and dt:
            cand = {"value": value, "unit": unit or "mg/m3", "dt": dt,
                    "raw": " | ".join(cells)}
            if best is None or cand["dt"] > best["dt"]:
                best = cand
    if best or not page_text:
        return best
    # fallback: parse the raw page text
    vals = re.findall(r"(\d+(?:\.\d+)?)\s*(mg/m³|mg/Nm³|µg/m³|ug/m³"
                      r"|mg/m3|ug/m3)", page_text, re.I)
    times = re.findall(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}[ ,]+\d{1,2}:\d{2}"
                       r"(?::\d{2})?(?:\s*[APap]\.?[Mm])?", page_text)
    if vals and times:
        dts = [d for d in (parse_dt(t) for t in times) if d]
        if dts:
            return {"value": vals[0][0], "unit": vals[0][1].replace("³", "3"),
                    "dt": max(dts), "raw": page_text[:200]}
    return None

# ---------------------------------------------------------------- main ------
def main():
    now = datetime.now(IST)
    payload, errors = scrape(best_proxies())
    if payload is None:
        send_text("❌ <b>CPCB Monitor — all attempts failed</b>\n" +
                  "\n".join(f"• {e[:350]}" for e in errors[:6]))
        print("FAILED:", errors)
        return

    rows, page_text = payload
    reading = pick_reading(rows, page_text)
    if reading is None:
        send_text("⚠️ <b>Page loaded but no emission reading parsed.</b>\n"
                  f"Page title: {page_text[:150]}")
        return

    na = is_na(reading["value"])
    when = reading["dt"].strftime("%d %b %Y, %I:%M %p IST")
    delay_h = (now - reading["dt"]).total_seconds() / 3600.0
    reasons = []
    if na:
        reasons.append("• Reading is <b>NA</b>")
    if delay_h >= DELAY_LIMIT_HOURS:
        reasons.append(f"• Timestamp <b>{delay_h:.1f}h</b> old")

    head = ("⚠️ <b>CPCB Emission Alert</b>" if reasons
            else "✅ <b>CPCB Monitor — healthy</b>")
    body = (f"{head}\n\n🏭 <b>Industry:</b> SIDDHI VINAYAK PROCESS\n"
            f"<b>Reading:</b> {'NA' if na else reading['value']} {reading['unit']}\n"
            f"<b>Last Received:</b> {when}\n<b>Data age:</b> {delay_h:.1f}h\n")
    if reasons:
        body += "\n<b>Triggers:</b>\n" + "\n".join(reasons) + "\n"
    body += f"\n🕒 Report: {now.strftime('%I:%M %p, %d %b %Y')} IST"

    send_photo("3_emissions.png" if os.path.exists("3_emissions.png") else None, body)
    print("Done. alert =", bool(reasons))

if __name__ == "__main__":
    main()
