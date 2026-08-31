#!/usr/bin/env python3
"""BudyCN — CodeBuddy.cn Account Creator (bundled core)
All-in-one module. No relative imports.
"""
import os, sys, json, time, re, hashlib, hmac, sqlite3, uuid
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-party
import requests
import urllib3
import tomllib
import platformdirs
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
from rich.align import Align
from rich import box
from rich.prompt import Prompt, Confirm, IntPrompt

urllib3.disable_warnings()

# ============================================================
# CONFIG LOADER
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.toml")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
_cfg = None

def load_config():
    global _cfg
    if _cfg is not None:
        return _cfg
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"config.toml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "rb") as f:
        _cfg = tomllib.load(f)
    return _cfg

def get_config():
    return load_config()

def get_5sim_jwt():
    cfg = load_config()
    if "otpcepat" in cfg:
        return cfg["otpcepat"].get("api_key", "")
    return cfg.get("5sim", {}).get("jwt", "")

def get_proxies():
    return load_config().get("proxies", [])

def get_bot_settings():
    return load_config().get("bot", {})

def get_router_settings():
    bot = load_config().get("bot", {})
    return bot.get("router", {})

def find_db_path():
    candidates = []
    router = get_router_settings()
    config_path = router.get("db_path", "")
    if config_path:
        candidates.append(os.path.expanduser(config_path))
    try:
        data_dir = platformdirs.user_data_dir("9router", appauthor=False)
        candidates.append(os.path.join(data_dir, "db", "data.sqlite"))
    except Exception:
        pass
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(os.path.join(appdata, "9router", "db", "data.sqlite"))
    if sys.platform != "win32":
        for base in ["~/.9router", "~/.config/9router", "~/.local/share/9router"]:
            candidates.append(os.path.join(os.path.expanduser(base), "db", "data.sqlite"))
    if sys.platform == "darwin":
        candidates.append(os.path.expanduser("~/Library/Application Support/9router/db/data.sqlite"))
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def get_data_file(filename):
    return os.path.join(DATA_DIR, filename)


# ============================================================
# 5SIM API CLIENT
# ============================================================
class FiveSimClient:
    BASE = "https://5sim.net/v1"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {get_5sim_jwt()}",
            "Accept": "application/json",
        })

    def get_balance(self):
        r = self.session.get(f"{self.BASE}/user/profile", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_price(self, country="hongkong", service="codebuddy"):
        r = self.session.get(f"{self.BASE}/guest/prices?country={country}", timeout=15)
        data = r.json()
        return data.get(service, {})

    def buy_number(self, country="hongkong", service="codebuddy"):
        r = self.session.get(
            f"{self.BASE}/user/buy/activation/{country}/any/{service}", timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def check_otp(self, order_id):
        r = self.session.get(f"{self.BASE}/user/check/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id):
        r = self.session.get(f"{self.BASE}/user/cancel/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def finish_order(self, order_id):
        r = self.session.get(f"{self.BASE}/user/finish/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_active_orders(self):
        r = self.session.get(
            f"{self.BASE}/user/orders?category=activation&limit=10", timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return [o for o in data.get("data", []) if o.get("status") in ("PENDING", "RECEIVED")]


# ============================================================
# OTPCEPAT API CLIENT (key from config)
# ============================================================
OTP_BASE = "https://otpcepat.org/api/handler_api.php"

def _otpcepat_key():
    cfg = load_config()
    return cfg.get("otpcepat", {}).get("api_key", "")

def _otp_get(action, **params):
    params["api_key"] = _otpcepat_key()
    params["action"] = action
    r = requests.get(OTP_BASE, params=params, timeout=30)
    try:
        return r.json()
    except Exception:
        r.raise_for_status()
        return {}

def otp_get_balance():
    return _otp_get("getBalance")

def otp_buy_number(country_id="14", operator_id="random", service_id="311"):
    return _otp_get("get_order", country_id=country_id, operator_id=operator_id, service_id=service_id)

def otp_check_status(order_id):
    return _otp_get("get_status", order_id=order_id)

def otp_set_status(order_id, status):
    return _otp_get("set_status", order_id=order_id, status=status)

def otp_cancel_order(order_id):
    return otp_set_status(order_id, 2)

def otp_finish_order(order_id):
    return otp_set_status(order_id, 4)

def otp_wait_for_otp(order_id, max_wait=120, interval=5):
    start = time.time()
    while time.time() - start < max_wait:
        result = otp_check_status(order_id)
        if result.get("status"):
            data = result.get("data", {})
            sms = data.get("sms", "")
            if sms and sms != "Waiting SMS":
                return sms
            status = data.get("status", "")
            if status in ("Cancel", "Done", "Failed"):
                return None
        time.sleep(interval)
    return None


# ============================================================
# AUTH HELPERS
# ============================================================
AUTH_HEADERS = {
    "User-Agent": "CLI/2.108.1 CodeBuddy/2.108.1",
    "X-Product": "SaaS",
    "X-IDE-Type": "CLI",
    "X-IDE-Name": "CLI",
    "X-Requested-With": "XMLHttpRequest",
    "X-Codebuddy-Request": "1",
    "X-Domain": "copilot.tencent.com",
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "Content-Type": "application/json",
}

def get_state():
    s = requests.Session()
    s.verify = False
    s.headers.update(AUTH_HEADERS)
    r = s.post(
        "https://copilot.tencent.com/v2/plugin/auth/state?platform=CLI",
        json={}, timeout=15,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get state: {data.get('msg', 'unknown')}")
    state = data["data"]["state"]
    auth_url = data["data"]["authUrl"]
    return state, auth_url

def build_keycloak_url(state):
    return (
        "https://www.codebuddy.cn/auth/realms/copilot/protocol/openid-connect/auth?"
        + urlencode({
            "client_id": "console",
            "redirect_uri": f"https://www.codebuddy.cn/login/?platform=CLI&state={state}",
            "response_type": "code",
            "scope": "openid",
        })
    )

def poll_token(state, max_attempts=25, interval=5):
    s = requests.Session()
    s.verify = False
    s.headers.update({
        **AUTH_HEADERS,
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        "Accept": "application/json",
    })
    for i in range(max_attempts):
        r = s.get(
            "https://copilot.tencent.com/v2/plugin/auth/token",
            params={"state": state}, timeout=10,
        )
        data = r.json()
        code = data.get("code", -1)
        if code == 0 and data.get("data", {}).get("accessToken"):
            d = data["data"]
            return {
                "accessToken": d["accessToken"],
                "refreshToken": d.get("refreshToken", ""),
                "expiresIn": d.get("expiresIn", 86400),
            }
        elif code == 11217:
            pass
        else:
            pass
        if i < max_attempts - 1:
            time.sleep(interval)
    return None


# ============================================================
# BROWSER AUTOMATION
# ============================================================
def run_login_flow(phone, country_code, local_number, order_id, state, fivesim, debug=False, proxy=None):
    settings = get_bot_settings()
    headless = settings.get("headless", True)
    debug = debug or settings.get("debug", False)
    keycloak_url = build_keycloak_url(state)
    otp_code = None

    pw_proxy = None
    if proxy:
        server = f"{proxy.get('type','http')}://{proxy.get('host','')}:{proxy.get('port','')}"
        pw_proxy = {"server": server}
        if proxy.get("username") and proxy.get("password"):
            pw_proxy["username"] = proxy["username"]
            pw_proxy["password"] = proxy["password"]

    with sync_playwright() as pw:
        launch_args = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--disable-setuid-sandbox",
                "--single-process", "--no-zygote", "--window-size=1920,1080",
            ],
        }
        if pw_proxy:
            launch_args["proxy"] = pw_proxy

        browser = pw.chromium.launch(**launch_args)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 800}, locale="zh-CN",
        )
        page = context.new_page()

        if debug:
            def on_response(resp):
                url = resp.url
                if any(kw in url for kw in ["token", "auth/login", "auth/state", "plugin"]):
                    try:
                        body = resp.json()
                        print(f"  [NET] {resp.status} {url[:100]} -> {json.dumps(body)[:150]}")
                    except Exception:
                        pass
            page.on("response", on_response)

        try:
            if debug:
                print("    Opening Keycloak...")
            page.goto(keycloak_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            if debug:
                page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_01_keycloak.png"))

            country_sel = page.locator(".kc-country-selector")
            if country_sel.count() > 0:
                country_sel.first.click()
                page.wait_for_timeout(500)
                country_option = page.locator(f".kc-country-option:has-text('+{country_code}')")
                if country_option.count() > 0:
                    country_option.first.click()
                else:
                    all_opts = page.locator(".kc-country-option")
                    for i in range(min(all_opts.count(), 10)):
                        txt = (all_opts.nth(i).text_content() or "").strip()
                        if country_code in txt:
                            all_opts.nth(i).click()
                            break
            page.wait_for_timeout(300)
            if debug:
                page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_01b_country.png"))

            phone_sel = page.locator("input#phoneNumber[type='text']")
            phone_sel.wait_for(state="visible", timeout=10000)
            phone_sel.fill(local_number)

            page.wait_for_timeout(500)
            send_btn = page.locator("input.code-btn")
            try:
                send_btn.wait_for(state="visible", timeout=5000)
                send_btn.click()
            except PwTimeout:
                btns = page.locator("input[type='button']")
                clicked = False
                for i in range(min(btns.count(), 10)):
                    val = (btns.nth(i).get_attribute("value") or "").strip()
                    if any(kw in val for kw in ["验证码", "获取", "send", "code"]):
                        btns.nth(i).click()
                        clicked = True
                        break
                if not clicked:
                    raise RuntimeError("Send SMS button not found")

            if debug:
                page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_03_sent.png"))
            page.wait_for_timeout(3000)

            start = time.time()
            while time.time() - start < 90:
                try:
                    resp = fivesim.check_otp(order_id)
                    if resp.get("sms"):
                        otp_code = resp["sms"][0].get("code")
                        if debug:
                            print(f"    OTP: {otp_code}")
                        break
                    st = resp.get("status", "")
                    if st in ("CANCELED", "TIMEOUT", "BANNED"):
                        if debug:
                            print(f"    Order status: {st}")
                        break
                except Exception as e:
                    if debug:
                        print(f"    Poll error: {e}")
                if debug:
                    print(f"    ... waiting ({int(time.time() - start)}s)")
                time.sleep(4)

            if not otp_code:
                fivesim.cancel_order(order_id)
                return None, None, None

            otp_sel = page.locator("input#code")
            otp_sel.wait_for(state="visible", timeout=5000)
            otp_sel.fill(otp_code)
            if debug:
                page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_04_otp_filled.png"))

            page.wait_for_timeout(500)
            submit_btn = page.locator("input#kc-login")
            submit_btn.wait_for(state="visible", timeout=5000)
            for _ in range(10):
                disabled = submit_btn.get_attribute("disabled")
                if disabled is None:
                    break
                page.wait_for_timeout(1000)
            submit_btn.click()

            page.wait_for_timeout(4000)
            post_url = page.url

            error_selectors = [
                ".kc-feedback-text", ".alert-error", ".pf-m-danger",
                "#kc-error-message", ".kc-alert", "[class*='error']",
                "[class*='alert']", ".help-block", ".invalid-feedback",
                "#input-error", ".pf-c-alert", ".pf-c-form__alert",
            ]
            has_error = False
            for sel in error_selectors:
                try:
                    el = page.locator(sel)
                    if el.count() > 0 and el.first.is_visible():
                        txt = (el.first.text_content() or "").strip()
                        if txt and len(txt) > 2:
                            if debug:
                                print(f"    ERROR [{sel}]: {txt[:200]}")
                            has_error = True
                except Exception:
                    pass

            if has_error:
                if debug:
                    page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_error.png"))
                fivesim.cancel_order(order_id)
                return None, None, None

            if "authenticate" in post_url or "login-actions" in post_url:
                if debug:
                    print("    WARNING: Still on authenticate page!")
                    with open(os.path.join(PROJECT_ROOT, "debug_authenticate.html"), "w", encoding="utf-8") as f:
                        f.write(page.content())
                otp_retry = page.locator("input#code")
                if otp_retry.count() > 0 and otp_retry.first.is_visible():
                    fivesim.cancel_order(order_id)
                    return None, None, None
                body_text = (page.locator("body").text_content() or "").lower()
                for kw in ["invalid", "expired", "incorrect", "wrong", "try again", "error", "failed", "失败", "错误", "无效", "过期"]:
                    if kw in body_text:
                        fivesim.cancel_order(order_id)
                        return None, None, None

            for i in range(20):
                page.wait_for_timeout(2000)
                url = page.url
                if "codebuddy.cn" in url and "/login" not in url.split("?")[0]:
                    break
                if "authenticate" not in url and "login-actions" not in url:
                    if "/login" in url:
                        page.wait_for_timeout(3000)
                        break

            if debug:
                page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_06_after_login.png"))

        except Exception as e:
            if debug:
                print(f"    FATAL: {e}")
                try:
                    page.screenshot(path=os.path.join(PROJECT_ROOT, "debug_FATAL.png"))
                except Exception:
                    pass
            try:
                fivesim.cancel_order(order_id)
            except Exception:
                pass
            raise
        finally:
            browser.close()

    return True


# ============================================================
# 9ROUTER DB INJECTION
# ============================================================
def inject_token(phone, access_token, refresh_token, expires_in):
    db_path = find_db_path()
    if not db_path or not os.path.exists(db_path):
        return False, None

    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda x: x.decode("utf-8", errors="replace")
    c = conn.cursor()

    new_id = str(uuid.uuid4())
    now = datetime.now().isoformat() + "Z"
    exp = expires_in or 86400
    expires_at = datetime.fromtimestamp(datetime.now().timestamp() + exp).isoformat() + "Z"

    data = json.dumps({
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "expiresIn": exp,
        "scope": "openid",
        "providerSpecificData": {
            "connectionProxyEnabled": False,
            "connectionProxyUrl": "",
            "connectionNoProxy": "",
        },
    })

    c.execute(
        """INSERT INTO providerConnections
           (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
           VALUES (?, 'codebuddy-cn', 'oauth', ?, NULL, 1, 1, ?, ?, ?)""",
        (new_id, f"Account {phone}", data, now, now),
    )
    conn.commit()
    conn.close()
    return True, new_id

def list_connections():
    db_path = find_db_path()
    if not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda x: x.decode("utf-8", errors="replace")
    c = conn.cursor()
    c.execute(
        "SELECT id, provider, authType, name, createdAt FROM providerConnections "
        "WHERE provider='codebuddy-cn' ORDER BY createdAt DESC"
    )
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "provider": r[1], "authType": r[2], "name": r[3], "createdAt": r[4]} for r in rows]


# ============================================================
# PROXY CHECKER
# ============================================================
def check_proxy(proxy_dict, timeout=8):
    label = proxy_dict.get("label", "unknown")
    proxy_type = proxy_dict.get("type", "http")
    host = proxy_dict.get("host", "")
    port = proxy_dict.get("port", 80)
    username = proxy_dict.get("username", "")
    password = proxy_dict.get("password", "")

    auth = f"{username}:{password}@" if username and password else ""
    proxy_url = f"{proxy_type}://{auth}{host}:{port}"
    proxies = {"http": proxy_url, "https": proxy_url}

    result = {
        "label": label, "working": False, "ip": None,
        "latency_ms": None, "error": None, "codebuddy_cn_reachable": False,
    }

    try:
        start = time.monotonic()
        r = requests.get("http://httpbin.org/ip", proxies=proxies, timeout=timeout)
        elapsed = (time.monotonic() - start) * 1000
        if r.status_code == 200:
            result["working"] = True
            result["ip"] = r.json().get("origin", "unknown")
            result["latency_ms"] = round(elapsed, 1)
    except requests.exceptions.Timeout:
        result["error"] = "Timeout"
    except requests.exceptions.ProxyError as e:
        result["error"] = f"ProxyError: {str(e)[:80]}"
    except requests.exceptions.ConnectionError:
        result["error"] = "Connection refused"
    except Exception as e:
        result["error"] = str(e)[:80]

    if result["working"]:
        try:
            r2 = requests.get("https://www.codebuddy.cn", proxies=proxies, timeout=timeout, verify=False)
            result["codebuddy_cn_reachable"] = r2.status_code == 200
        except Exception:
            result["codebuddy_cn_reachable"] = False

    return result

def check_all_proxies(proxy_list, max_workers=5):
    if not proxy_list:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(proxy_list))) as executor:
        futures = {executor.submit(check_proxy, p): p for p in proxy_list}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                p = futures[future]
                results.append({
                    "label": p.get("label", "unknown"), "working": False, "ip": None,
                    "latency_ms": None, "error": str(e)[:80], "codebuddy_cn_reachable": False,
                })
    results.sort(key=lambda r: r["label"])
    return results


# ============================================================
# RICH UI
# ============================================================
console = Console()

def print_banner():
    console.print()
    banner = (
        "  ___ ___  ___  ___ ___ _   _ ___  _____   __\n"
        " / __/ _ \\|   \\| __| _ ) | | |   \\|   \\ \\ / /\n"
        "| (_| (_) | |) | _|| _ \\ |_| | |) | |) \\ V / \n"
        " \\___\\___/|___/|___|___/\\___/|___/|___/ |_|  \n\n"
        "Account Creator\n\n"
        "by Dava"
    )
    console.print(Panel(
        Align.center(banner), box=box.SIMPLE, padding=(1, 2),
    ))
    console.print()

def print_balance(balance_data):
    bal = balance_data.get("balance", "N/A")
    email = balance_data.get("email", "N/A")
    acc_id = balance_data.get("id", "N/A")
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("Balance", f"${bal}")
    table.add_row("Email", str(email))
    table.add_row("Account ID", str(acc_id))
    console.print(Panel(table, title="5SIM Account", box=box.SIMPLE))
    console.print()

def print_price(price_data):
    if not price_data:
        console.print("  Price data not available")
        return
    price = price_data.get("price", "N/A")
    stock = price_data.get("count", "N/A")
    console.print(f"  HK CodeBuddy: ${price}  |  Stock: {stock}")
    console.print()

def print_proxy_status(proxy_results):
    table = Table(title="Proxy Status", box=box.SIMPLE)
    table.add_column("Label")
    table.add_column("Status")
    table.add_column("IP")
    table.add_column("Latency", justify="right")
    table.add_column("CN Reachable")
    for r in proxy_results:
        status = "OK" if r["working"] else "FAIL"
        ip = r.get("ip") or "-"
        latency = f"{r['latency_ms']}ms" if r.get("latency_ms") else "-"
        cn = "Yes" if r.get("codebuddy_cn_reachable") else "No"
        if r.get("error"):
            cn = r["error"][:40]
        table.add_row(r["label"], status, ip, latency, cn)
    console.print(table)
    console.print()

def print_account_summary(accounts):
    table = Table(title="Batch Summary", box=box.SIMPLE)
    table.add_column("Phone")
    table.add_column("Status")
    table.add_column("Error")
    ok_count = sum(1 for a in accounts if a["status"] == "OK")
    fail_count = len(accounts) - ok_count
    for a in accounts:
        table.add_row(a.get("phone", "N/A"), a["status"], a.get("error", "") or "")
    console.print(table)
    console.print(Panel(f"{ok_count} OK | {fail_count} FAIL", title="Result", box=box.SIMPLE))
    console.print()

def print_tokens(tokens):
    if not tokens:
        console.print("  No tokens saved yet.")
        return
    table = Table(title="Saved Tokens", box=box.SIMPLE)
    table.add_column("#")
    table.add_column("Phone")
    table.add_column("Access Token", no_wrap=True, max_width=60)
    table.add_column("Expires In", justify="right")
    for i, t in enumerate(tokens, 1):
        at = t.get("accessToken", "")[:50] + "..."
        exp = t.get("expiresIn", "N/A")
        table.add_row(str(i), t.get("phone", "N/A"), at, str(exp))
    console.print(table)
    console.print(f"  Total: {len(tokens)} account(s)")
    console.print()

def create_progress():
    return Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"), TimeRemainingColumn(), console=console,
    )

def print_success(message):
    console.print(f"  OK  {message}")

def print_error(message):
    console.print(f"  ERR {message}")

def print_info(message):
    console.print(f"  --> {message}")

def print_settings(settings):
    table = Table(title="Settings", box=box.SIMPLE)
    table.add_column("Key")
    table.add_column("Value")
    for k, v in settings.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                table.add_row(f"{k}.{sk}", str(sv))
        elif isinstance(v, list):
            table.add_row(k, f"{len(v)} items")
        else:
            table.add_row(k, str(v))
    console.print(table)
    console.print()

def print_menu(menu_items):
    menu_text = ""
    for key, (label, _) in menu_items.items():
        menu_text += f"  [{key}]  {label}\n"
    panel = Panel(menu_text.rstrip(), title="MENU", subtitle="select option", box=box.SIMPLE, padding=(1, 2))
    console.print(panel)
    console.print()


# ============================================================
# CLI MENU & ROUTING
# ============================================================
def menu_balance():
    fivesim = FiveSimClient()
    try:
        bal = fivesim.get_balance()
        print_balance(bal)
        price = fivesim.get_price("hongkong", "codebuddy")
        print_price(price)
    except Exception as e:
        print_error(f"Failed to check balance: {e}")

def menu_proxy():
    cfg = get_config()
    proxies = cfg.get("proxies", [])
    if not proxies:
        print_info("No proxies configured in config.toml")
        return
    print_info(f"Testing {len(proxies)} proxy(s)...")
    results = check_all_proxies(proxies)
    print_proxy_status(results)
    working = [r for r in results if r["working"]]
    cn_ok = [r for r in working if r["codebuddy_cn_reachable"]]
    print_info(f"Working: {len(working)}/{len(proxies)} | CN reachable: {len(cn_ok)}/{len(proxies)}")

def _create_single_account(fivesim_client, debug=False):
    bal = fivesim_client.get_balance()
    bal_amount = float(bal["balance"])
    if bal_amount < 0.13:
        return {"phone": "N/A", "status": "FAIL", "error": "Insufficient balance"}

    order = fivesim_client.buy_number()
    oid = order["id"]
    phone_raw = order["phone"]
    phone = "+" + phone_raw.lstrip("+")
    phone_clean = phone.lstrip("+")
    country_code = phone_clean[:3] if phone_clean.startswith("852") else phone_clean[:2]
    local_number = phone_clean[len(country_code):]

    print_info(f"Phone: {phone} | Order: {oid} | Price: ${order['price']}")

    state, _ = get_state()
    print_info("Running browser login flow...")
    try:
        success = run_login_flow(
            phone=phone, country_code=country_code, local_number=local_number,
            order_id=oid, state=state, fivesim=fivesim_client, debug=debug,
        )
        if not success:
            return {"phone": phone, "status": "FAIL", "error": "Login flow failed"}

        print_info("Polling token...")
        token = poll_token(state)
        if not token:
            fivesim_client.cancel_order(oid)
            return {"phone": phone, "status": "FAIL", "error": "Token polling timeout"}

        ok, conn_id = inject_token(phone, token["accessToken"], token["refreshToken"], token["expiresIn"])
        if ok:
            print_success(f"Injected to 9router: {conn_id}")
        else:
            print_info("9router DB not found, skipping injection")

        _save_token(phone, state, token)
        fivesim_client.finish_order(oid)
        print_success(f"Account created: {phone}")
        return {"phone": phone, "status": "OK", "error": None}

    except Exception as e:
        try:
            fivesim_client.cancel_order(oid)
        except Exception:
            pass
        return {"phone": phone, "status": "FAIL", "error": str(e)[:100]}

def menu_create():
    fivesim = FiveSimClient()
    try:
        bal = fivesim.get_balance()
        print_balance(bal)
    except Exception as e:
        print_error(f"Cannot check balance: {e}")
        return
    confirm = Confirm.ask("Create one account?", default=True)
    if not confirm:
        return
    result = _create_single_account(fivesim, debug=False)
    if result["status"] == "OK":
        print_success(f"Done! Account: {result['phone']}")
    else:
        print_error(f"Failed: {result['error']}")

def menu_batch():
    fivesim = FiveSimClient()
    try:
        bal = fivesim.get_balance()
        bal_amount = float(bal["balance"])
        print_balance(bal)
    except Exception as e:
        print_error(f"Cannot check balance: {e}")
        return
    max_accounts = int(bal_amount // 0.13)
    print_info(f"Balance: ${bal_amount} -- max ~{max_accounts} accounts (@ $0.13 each)")
    count = IntPrompt.ask("How many accounts?", default=1, show_default=True)
    if count <= 0:
        return
    estimated = count * 0.13
    confirm = Confirm.ask(
        f"Create {count} account(s)? Estimated cost: ${estimated:.2f}",
        default=True,
    )
    if not confirm:
        return
    results = []
    with create_progress() as progress:
        task = progress.add_task("Creating accounts...", total=count)
        for i in range(count):
            client = FiveSimClient()
            try:
                result = _create_single_account(client, debug=False)
                results.append(result)
            except Exception as e:
                results.append({"phone": "N/A", "status": "FAIL", "error": str(e)[:100]})
            progress.update(task, advance=1)
    print_account_summary(results)
    try:
        final_bal = fivesim.get_balance()
        print_info(f"Final balance: ${final_bal['balance']}")
    except Exception:
        pass

def menu_tokens():
    token_file = get_data_file("oauth_tokens.json")
    if not os.path.exists(token_file):
        print_info("No tokens file found.")
        return
    with open(token_file, "r", encoding="utf-8") as f:
        try:
            tokens = json.load(f)
        except json.JSONDecodeError:
            tokens = []
    if not isinstance(tokens, list):
        tokens = [tokens]
    print_tokens(tokens)

def menu_settings():
    cfg = get_config()
    settings = {
        "5sim.jwt": cfg.get("5sim", {}).get("jwt", "")[:30] + "...",
        "proxies": [p.get("label", "?") for p in cfg.get("proxies", [])],
        "bot.country": get_bot_settings().get("country", "N/A"),
        "bot.service": get_bot_settings().get("service", "N/A"),
        "bot.headless": get_bot_settings().get("headless", True),
        "bot.debug": get_bot_settings().get("debug", False),
        "bot.target_keys": get_bot_settings().get("target_keys", 10),
        "bot.router.provider": get_router_settings().get("provider", "N/A"),
        "bot.router.db_path": get_router_settings().get("db_path", "N/A"),
    }
    db_path = find_db_path()
    settings["db.resolved"] = db_path or "NOT FOUND"
    print_settings(settings)

def _save_token(phone, state, token):
    token_file = get_data_file("oauth_tokens.json")
    if os.path.exists(token_file):
        with open(token_file, "r", encoding="utf-8") as f:
            try:
                all_tokens = json.load(f)
            except json.JSONDecodeError:
                all_tokens = []
        if not isinstance(all_tokens, list):
            all_tokens = [all_tokens] if all_tokens else []
    else:
        all_tokens = []
    all_tokens.append({
        "phone": phone, "state": state,
        "accessToken": token["accessToken"],
        "refreshToken": token["refreshToken"],
        "expiresIn": token["expiresIn"],
    })
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(all_tokens, f, indent=2, ensure_ascii=False)

def run_interactive():
    menu_items = {
        "1": ("Check Balance", menu_balance),
        "2": ("Check Proxies", menu_proxy),
        "3": ("Create Account", menu_create),
        "4": ("Batch Create", menu_batch),
        "5": ("View Tokens", menu_tokens),
        "6": ("Settings", menu_settings),
        "7": ("Exit", None),
    }
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print_banner()
        print_menu(menu_items)
        choice = Prompt.ask("Select option", choices=list(menu_items.keys()), default="1")
        if choice == "7":
            console.print()
            print_info("Goodbye!")
            break
        _, handler = menu_items[choice]
        if handler:
            console.print()
            handler()
            console.print()
            Prompt.ask("[dim]Press Enter to continue[/dim]", default="", show_default=False)


# ============================================================
# MAIN ENTRY POINT
# ============================================================
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_interactive()


if __name__ == "__main__":
    main()
