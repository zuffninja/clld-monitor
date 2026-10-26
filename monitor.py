"""
CLLD monitor: sprawdza "Recent sentiment turns" na CoinGlass (Liquidation Levels)
i wysyla powiadomienie na telefon (ntfy), gdy pojawi sie nowy Bullish/Bearish turn.

Zmienne srodowiskowe:
  NTFY_TOPIC   - nazwa Twojego kanalu w aplikacji ntfy (wymagane)
  SYMBOLS      - lista coinow po przecinku, domyslnie "BTC"
  INTERVAL     - co ile sekund sprawdzac; 0 = jedno sprawdzenie i koniec (GitHub Actions)
  STATE_FILE   - plik ze stanem, domyslnie "state.json"
  TZ_NAME      - strefa czasowa godzin na stronie, domyslnie "Europe/Warsaw"
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "BTC").split(",") if s.strip()]
INTERVAL = int(os.environ.get("INTERVAL", "0") or 0)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Warsaw")
ERROR_ALERT_AFTER = 30 * 60  # powiadom o awarii po 30 min samych bledow
PAGE_RECYCLE_EVERY = 60      # co tyle sprawdzen otworz przegladarke na nowo (pamiec)

TURN_RE = re.compile(r"(Bullish|Bearish) turn\s+(\d{2}-\d{2} \d{2}:\d{2})")
PRICE_RE = re.compile(r"Last price\s+([\d,]+(?:\.\d+)?)")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def notify(title, message, tags):
    if not NTFY_TOPIC:
        log("[ntfy] brak NTFY_TOPIC, pomijam:", title, "|", message)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Priority": "high", "Tags": tags},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log("[ntfy]", r.status, title)
    except Exception as e:  # noqa: BLE001
        log("[ntfy] blad wysylki:", e)


def turn_tags(turn):
    return "green_circle,chart_with_upwards_trend" if turn.startswith("Bullish") else "red_circle,chart_with_downwards_trend"


def read_turns(page, symbol):
    url = f"https://www.coinglass.com/liquidation-levels?symbol={symbol}"
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function(
        "() => /(Bullish|Bearish) turn\\s+\\d{2}-\\d{2}/.test(document.body.innerText)",
        timeout=90_000,
    )
    page.wait_for_timeout(2000)
    text = page.evaluate("() => document.body.innerText")
    turns = []
    for kind, ts in TURN_RE.findall(text):
        key = f"{kind} {ts}"
        if key not in turns:
            turns.append(key)
    m = PRICE_RE.search(text)
    return turns, (m.group(1) if m else None), url


def check_symbol(page, symbol, state):
    turns, price, url = read_turns(page, symbol)
    log(f"[{symbol}] {turns[:2]} | cena {price}")
    seen = state.get(symbol)
    if seen is None:
        # pierwsze uruchomienie: zapamietaj historie, nie spamuj
        state[symbol] = turns
        if turns:
            notify(
                f"CLLD monitor {symbol} wystartowal",
                f"Ostatni sygnal: {turns[0]}" + (f"\nCena: {price}" if price else ""),
                "white_check_mark",
            )
        return
    new = [t for t in turns if t not in seen]
    for t in reversed(new):  # od najstarszego
        kind = "BULLISH TURN" if t.startswith("Bullish") else "BEARISH TURN"
        body = t + (f"\nCena: {price}" if price else "") + f"\n{url}"
        notify(f"{symbol} CLLD {kind}", body, turn_tags(t))
    state[symbol] = (turns + [t for t in seen if t not in turns])[:50]


def new_page(p):
    browser = p.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])
    ctx = browser.new_context(
        timezone_id=TZ_NAME, locale="en-US",
        viewport={"width": 1600, "height": 1000}, user_agent=UA,
    )
    return browser, ctx.new_page()


def run_once():
    state = load_state()
    errors = 0
    with sync_playwright() as p:
        browser, page = new_page(p)
        for symbol in SYMBOLS:
            try:
                check_symbol(page, symbol, state)
            except Exception as e:  # noqa: BLE001
                errors += 1
                log(f"[{symbol}] blad odczytu: {e}")
        browser.close()
    save_state(state)
    if errors == len(SYMBOLS):
        sys.exit(1)


def run_loop():
    log(f"Start: {SYMBOLS}, co {INTERVAL}s")
    state = load_state()
    last_ok = time.time()
    error_alert_sent = False
    with sync_playwright() as p:
        browser, page, checks = None, None, 0
        while True:
            started = time.time()
            try:
                if browser is None or checks >= PAGE_RECYCLE_EVERY:
                    if browser is not None:
                        browser.close()
                    browser, page = new_page(p)
                    checks = 0
                for symbol in SYMBOLS:
                    check_symbol(page, symbol, state)
                save_state(state)
                checks += 1
                last_ok = time.time()
                if error_alert_sent:
                    notify("CLLD monitor znowu dziala", "Odczyt CoinGlass wrocil do normy.", "white_check_mark")
                    error_alert_sent = False
            except Exception as e:  # noqa: BLE001
                log("blad:", e)
                try:
                    if browser is not None:
                        browser.close()
                except Exception:  # noqa: BLE001
                    pass
                browser = None
                if not error_alert_sent and time.time() - last_ok > ERROR_ALERT_AFTER:
                    notify("CLLD monitor: problem",
                           "Od 30 min nie udaje sie odczytac CoinGlass. Sprawdz serwer / strone.",
                           "warning")
                    error_alert_sent = True
            time.sleep(max(5, INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    run_loop() if INTERVAL > 0 else run_once()
