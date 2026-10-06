import concurrent.futures
import json
import os
import re
import time
from datetime import datetime

import pytz
import requests
import urllib3
from playwright.sync_api import sync_playwright

urllib3.disable_warnings()

IST = pytz.timezone("Asia/Kolkata")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DASH_URL = os.environ.get(
    "DASH_URL",
    "https://cems.cpcb.gov.in/public/#/l/dashboard/site-info/"
    "eyJvYmoiOiJpbmR1c3RyeV83MjU3In0=",
)
DELAY_LIMIT_HOURS = float(os.environ.get("DELAY_LIMIT_HOURS", "1"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "90000"))
PROXY_SERVER = os.environ.get("CEMS_PROXY")          # http://user:pass@host:port
DISCOVER = os.environ.get("DISCOVER", "0") == "1"
ALWAYS_NOTIFY = os.environ.get("ALWAYS_NOTIFY", "1") == "1"
BLOCK_ASSETS = os.environ.get("BLOCK_ASSETS", "0") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

FREE_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http"
    "&timeout=10000&country=IN&ssl=all&anonymity=all",
    "https://www.proxy-list.download/api/v1/get?type=http&country=IN",
    "https://www.proxy-list.download/api/v1/get?type=https&country=IN",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
]

# ---------------------------------------------------------------- telegram ---
def tg(method, **kwargs):
    if not BOT_TOKEN or not CHAT_ID:
        print("!! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notify")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                          timeout=30, **kwargs)
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            print(f"!! Telegram {method} failed: {r.status_code} {r.text[:300]}")
        return ok
    except Exception as e:
        print(f"!! Telegram {method} error: {e}")
        return False


def send_text(msg):
    return tg("sendMessage", json={
        "chat_id": CHAT_ID, "text": msg[:4096],
        "parse_mode": "HTML", "disable_web_page_preview": True})


def send_photo(path, caption=""):
    if not path or not os.path.exists(path):
        return send_text(caption)
    try:
        with open(path, "rb") as fh:
            return tg("sendPhoto",
                      data={"chat_id": CHAT_ID, "caption": caption[:1024],
                            "parse_mode": "HTML"},
                      files={"photo": fh})
    except Exception as e:
        print(f"!! sendPhoto error: {e}")
        return send_text(caption)


# ----------------------------------------------------------------- proxies ---
def fetch_free_proxies():
    found = set()
    for src in FREE_PROXY_SOURCES:
        try:
            r = requests.get(src, timeout=8)
            if r.status_code == 200:
                for m in re.finditer(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b", r.text):
                    found.add("http://" + m.group(0))
        except Exception:
            continue
    return list(found)


def check_proxy(p):
    try:
        r = requests.get("https://cems.cpcb.gov.in/public/",
                         proxies={"http": p, "https": p},
                         timeout=8, verify=False)
        return p if r.status_code == 200 else None
    except Exception:
        return None


def good_proxies(n=8):
    cands = fetch_free_proxies()
    print(f"Testing {len(cands)} free Indian proxies (8s each)…")
    out = []
    if not cands:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(check_proxy, p): p for p in cands}
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            if res:
                out.append(res)
                print(f"  ok: {res}")
                if len(out) >= n:
                    break
    return out


# ------------------------------------------------------------ browser wiretap
def capture(proxy_server):
    """Return list of (url, body) for every XHR/fetch response seen."""
    captured = []
    with sync_playwright() as p:
        kw = dict(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--ignore-certificate-errors"])
        if proxy_server:
            kw["proxy"] = {"server": proxy_server}
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(user_agent=UA,
                                  viewport={"width": 1920, "height": 1080},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        if BLOCK_ASSETS:
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font")
                       else route.continue_())

        def on_response(resp):
            try:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                txt = resp.text()
                if txt and ("{" in txt or "[" in txt):
                    captured.append((resp.url, txt))
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(DASH_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            print(f"goto warning: {e}")

        # best-effort: open the Emissions tab so its XHR fires
        for sel in ["text=/emission/i", "[role=tab]:has-text('Emission')",
                    "a:has-text('Emission')"]:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.click(timeout=5000)
                    page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        deadline = time.time() + 60
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            if captured:
                break

        browser.close()
    return captured


# ---------------------------------------------------------------- parsing ----
VALUE_KEYS = ("value", "reading", "val", "measuredvalue", "finalvalue",
              "concentration", "avg", "average", "result")
UNIT_KEYS = ("unit", "units", "uom")
TIME_KEYS = ("timestamp", "datetime", "receivedtime", "lastreceived",
             "last_received", "lttime", "sttime", "time", "date")
PARAM_KEYS = ("parameter", "param", "parametername", "pollutant", "name")


def norm_unit(u):
    if not u:
        return ""
    return (str(u).lower().replace("µ", "u").replace("μ", "u")
            .replace("³", "3").replace(" ", ""))


def is_na(v):
    if v is None:
        return True
    return str(v).strip().lower() in (
        "", "na", "n/a", "null", "none", "nan", "-", "--", "not available")


def get_field(d, keys):
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def walk(node, out):
    if isinstance(node, dict):
        vk = any(k in {str(x).lower() for x in node} for k in VALUE_KEYS)
        uk = any(k in {str(x).lower() for x in node} for k in UNIT_KEYS)
        if vk and uk:
            out.append(node)
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def parse_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{10,13}", v.strip())):
        n = float(v)
        if n > 1e12:           # milliseconds
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, IST)
        except Exception:
            return None
    s = str(v).strip().replace("/", "-")
    try:
        from dateutil import parser as dparser
        dt = dparser.parse(s, dayfirst=True)
        return IST.localize(dt) if dt.tzinfo is None else dt.astimezone(IST)
    except Exception:
        return None


def parse_all(captured):
    readings = []
    for url, text in captured:
        try:
            data = json.loads(text)
        except Exception:
            continue
        bucket = []
        walk(data, bucket)
        for rec in bucket:
            readings.append((url, rec))
    return readings


def choose_emission(readings):
    """Pick the most emissions-like mg/m3 record (paired value+unit+time)."""
    best = None
    for url, rec in readings:
        nu = norm_unit(get_field(rec, UNIT_KEYS))
        if "mg/m3" not in nu and "ug/m3" not in nu and "mg/nm3" not in nu:
            continue
        name = get_field(rec, PARAM_KEYS) or ""
        score = 2 if "mg/m3" in nu else 1
        if parse_dt(get_field(rec, TIME_KEYS)):
            score += 3
        if any(p in str(name).lower() for p in
               ("pm", "so2", "nox", "co", "stack", "spm", "tpm")):
            score += 1
        cand = {"value": get_field(rec, VALUE_KEYS), "unit": get_field(rec, UNIT_KEYS),
                "dt": parse_dt(get_field(rec, TIME_KEYS)), "name": name,
                "url": url, "score": score}
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


# --------------------------------------------------------------- screenshot --
def render_local(html, out="dashboard_snapshot.png"):
    path = os.path.abspath("local_dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                                  args=["--no-sandbox", "--disable-dev-shm-usage"])
            pg = b.new_page(viewport={"width": 1000, "height": 700})
            pg.goto("file://" + path)
            pg.screenshot(path=out, full_page=True)
            b.close()
        return out
    except Exception as e:
        print(f"screenshot failed: {e}")
        return None


def make_html(value, when, is_alert):
    color = "#d9363e" if is_alert else "#52c41a"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#f0f2f5;padding:40px;margin:0}}
.card{{background:#fff;border-radius:12px;padding:28px;max-width:760px;margin:auto;
box-shadow:0 8px 24px rgba(0,0,0,.1);border-top:6px solid #1890ff}}
h1{{color:#1890ff;font-size:24px;margin:0 0 16px}}
.w{{display:flex;gap:20px}} .b{{flex:1;background:#fafafa;border:1px solid #e8e8e8;
border-radius:8px;padding:18px;text-align:center}}
.t{{font-size:12px;color:#888;text-transform:uppercase;letter-spacing:1px}}
.v{{font-size:32px;font-weight:700;color:{color}}}
</style></head><body><div class="card">
<h1>🏭 SIDDHI VINAYAK PROCESS — CEMS</h1>
<div class="w"><div class="b"><div class="t">Emission</div><div class="v">{value}</div></div>
<div class="b"><div class="t">Last Received</div>
<div class="v" style="font-size:20px">{when}</div></div></div></div></body></html>"""


# -------------------------------------------------------------------- main ---
def dump_discovery(captured):
    lines = []
    for url, body in captured:
        endpoint = url.split("?")[0]
        lines.append(f"{url}\n  -> {body[:800]}\n")
    text = "\n".join(lines) or "NO XHR/fetch responses captured."
    with open("discovered_endpoints.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(text[:6000])
    # also ship the top candidates straight to Telegram
    seen = []
    for url, _ in captured:
        e = url.split("?")[0]
        if e not in seen:
            seen.append(e)
    if seen:
        send_text("🔎 <b>XHR endpoints seen</b>\n" + "\n".join(f"<code>{e}</code>" for e in seen[:25]))


def main():
    proxies = [PROXY_SERVER] if PROXY_SERVER else good_proxies(8)
    if not proxies:
        print("No proxy available — trying direct (works only from an Indian IP).")
        proxies = [None]

    captured = []
    for i, prox in enumerate(proxies, 1):
        print(f"--- attempt {i}/{len(proxies)}  proxy={prox} ---")
        try:
            captured = capture(prox)
        except Exception as e:
            print(f"capture failed: {e}")
            captured = []
        if captured:
            break

    if DISCOVER:
        dump_discovery(captured)
        return

    now = datetime.now(IST)
    reading = choose_emission(parse_all(captured))

    if reading is None:
        send_text(
            "⚠️ <b>CPCB Monitor: no emission reading found</b>\n\n"
            f"Captured <b>{len(captured)}</b> XHR responses, none with an mg/m³ value.\n"
            "Re-run with <code>DISCOVER=1</code> to dump the payloads.")
        print("FAILED: no mg/m3 reading parsed.")
        return

    value, dt = reading["value"], reading["dt"]
    na = is_na(value)
    when = dt.strftime("%d %b %Y, %I:%M %p IST") if dt else "Unknown"

    reasons = []
    if na:
        reasons.append(f"• Reading is <b>NA</b> ({reading['unit'] or 'mg/m³'})")
    if dt is None:
        reasons.append("• Could not read the sample timestamp")
    else:
        delay = (now - dt).total_seconds() / 3600.0
        if delay >= DELAY_LIMIT_HOURS:
            reasons.append(f"• Telemetry delayed <b>{int(delay)}h "
                           f"{int((delay % 1) * 60)}m</b> (last: {when})")

    is_alert = bool(reasons)
    head = "⚠️ <b>CPCB Emission Alert</b>" if is_alert else "✅ <b>CPCB Monitor — healthy</b>"
    body = (f"{head}\n\n🏭 <b>Industry:</b> SIDDHI VINAYAK PROCESS\n\n"
            f"<b>Reading:</b> {'NA' if na else str(value)} {reading['unit'] or 'mg/m³'}\n"
            f"<b>Last Received:</b> {when}\n")
    if reading.get("name"):
        body += f"<b>Parameter:</b> {reading['name']}\n"
    if reasons:
        body += "\n<b>Triggers:</b>\n" + "\n".join(reasons) + "\n"
    body += f"\n🕒 Report: {now.strftime('%I:%M %p, %d %b %Y')} IST"

    if not is_alert and not ALWAYS_NOTIFY:
        print("Healthy and ALWAYS_NOTIFY=0 — no message sent.")
        return

    shot = render_local(make_html(("NA" if na else str(value)), when, is_alert))
    send_photo(shot, body) if shot else send_text(body)
    print(f"Sent. alert={is_alert} na={na} delay_ok={not reasons}")


if __name__ == "__main__":
    main()
