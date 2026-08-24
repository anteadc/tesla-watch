import html
import json
import os
import time
import requests

STATE_FILE = "seen_vins.json"

# Job-length settings. Keep LOOP_SECONDS under the cron interval so runs do
# not pile up in the concurrency queue and get cancelled.
LOOP_SECONDS = 240          # 4 minutes of polling per run, cron fires every 5
POLL_EVERY_SECONDS = 30

# Telegram guardrails.
MAX_ALERTS_PER_POLL = 12    # anything beyond this is marked seen + summarised
SECONDS_BETWEEN_ALERTS = 2  # stays well under Telegram's ~20 msg/min limit
FAIL_ALERT_AFTER = 3        # consecutive all-model fetch failures before pinging

URL = "https://www.tesla.com/inventory/api/v4/inventory-results"

# UNVERIFIED against the live UAE endpoint. If runs log "HTTP 4xx from Tesla",
# this pair is the first thing to check.
MARKET = "AE"
SUPER_REGION = "north america"

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TEST_MESSAGE = os.environ.get("TEST_MESSAGE", "false").lower() == "true"

MODELS = {
    "my": "Model Y",
    "m3": "Model 3",
    "ms": "Model S",
    "mx": "Model X",
}

# Set to "my" to only alert on Model Y. None = alert on any model.
NOTIFY_ONLY_MODEL = None


# ---------------------------------------------------------------- Tesla fetch

def build_query(model_code):
    return {
        "query": {
            "model": model_code,
            "condition": "used",
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            "market": MARKET,
            "language": "en",
            "super_region": SUPER_REGION,
            "PaymentType": "cash",
            "paymentRange": "0,999999",
        },
        "offset": 0,
        "count": 50,
        "outsideOffset": 0,
        "outsideSearch": False,
        "isFalconDeliverySelectionEnabled": False,
        "version": None,
    }


def fetch_inventory(model_code):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.tesla.com/en_AE/inventory/used/{model_code}",
    }
    resp = requests.get(
        URL,
        params={"query": json.dumps(build_query(model_code))},
        headers=headers,
        timeout=20,
    )
    # Surface the real reason instead of a bare raise_for_status, so a 403 from
    # the GitHub runner IP is readable straight from the Actions log.
    if resp.status_code != 200:
        raise RuntimeError(
            f"HTTP {resp.status_code} from Tesla: {resp.text[:300]}"
        )
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response from Tesla: {resp.text[:300]}")


def collect_listings():
    """Returns (cars, failed_model_names, ok_model_codes).

    cars is a list of (model_code, model_name, car_dict).
    """
    cars = []
    failed = []
    ok = []
    for model_code, model_name in MODELS.items():
        try:
            data = fetch_inventory(model_code)
        except Exception as e:
            print(f"[{model_name}] fetch failed: {e}")
            failed.append(model_name)
            continue
        results = data.get("results", []) if isinstance(data, dict) else []
        print(f"[{model_name}] {len(results)} listings returned")
        ok.append(model_code)
        for car in results:
            if isinstance(car, dict):
                cars.append((model_code, model_name, car))
    return cars, failed, ok


# ---------------------------------------------------------------------- state

def load_state():
    """seeded_models tracks which models already have a baseline, so a model
    that was unreachable on an earlier run gets baselined silently later
    instead of dumping its whole inventory into your chat."""
    default = {"seen": set(), "seeded_models": set(), "fail_streak": 0}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
    except Exception as e:
        print(f"State file unreadable, starting fresh: {e}")
        return default

    if isinstance(raw, list):  # old format
        return {"seen": set(raw), "seeded_models": set(), "fail_streak": 0}

    return {
        "seen": set(raw.get("seen") or []),
        "seeded_models": set(raw.get("seeded_models") or []),
        "fail_streak": int(raw.get("fail_streak") or 0),
    }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    payload = {
        "seen": sorted(state["seen"]),
        "seeded_models": sorted(state["seeded_models"]),
        "fail_streak": state["fail_streak"],
    }
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ------------------------------------------------------------------- Telegram

def _deliver(method, payload):
    """Returns (ok, retry_after_seconds_or_None)."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            data=payload,
            timeout=20,
        )
    except Exception as e:
        print(f"Telegram network error on {method}: {e}")
        return False, 5

    if resp.status_code == 429:
        wait = 5
        try:
            wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
        except Exception:
            pass
        print(f"Telegram rate limited on {method}, waiting {wait}s")
        return False, wait

    if resp.ok:
        try:
            if resp.json().get("ok"):
                return True, None
        except Exception:
            pass

    print(f"Telegram {method} failed: {resp.status_code} {resp.text[:200]}")
    return False, None


def send_telegram(text, photo_url=None):
    """Returns True only on a confirmed successful send."""
    for _ in range(4):
        if photo_url:
            ok, retry = _deliver("sendPhoto", {
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": photo_url,
                "caption": text,
                "parse_mode": "HTML",
            })
            if ok:
                return True
            if retry:
                time.sleep(retry)
                continue
            print("Photo send failed permanently, falling back to text.")
            photo_url = None

        ok, retry = _deliver("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if ok:
            return True
        if retry:
            time.sleep(retry)
            continue
        return False
    return False


# ------------------------------------------------------------ car parsing

def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def safe(fn, default="Unknown"):
    try:
        result = fn()
        return result if result not in (None, "") else default
    except Exception:
        return default


def find_option_descriptions(car, group_name):
    found = []
    try:
        for opt in car.get("OptionCodeData") or []:
            if not isinstance(opt, dict):
                continue
            if opt.get("group") == group_name:
                desc = opt.get("description") or opt.get("long_name") or opt.get("name")
                if desc:
                    found.append(desc)
    except Exception:
        pass
    return found


def short_label(text, keyword_map, fallback=None):
    if fallback is None:
        fallback = text if text else "Unknown"
    if not text:
        return fallback
    upper = str(text).upper()
    for keyword, label in keyword_map:
        if keyword in upper:
            return label
    return fallback


EXTERIOR_KEYWORDS = [
    ("RED", "Red"), ("BLUE", "Blue"), ("WHITE", "White"), ("BLACK", "Black"),
    ("GREY", "Grey"), ("GRAY", "Grey"), ("SILVER", "Silver"), ("GREEN", "Green"),
]
INTERIOR_KEYWORDS = [
    ("BLACK", "Black"), ("WHITE", "White"), ("CREAM", "Cream"),
]
TRIM_KEYWORDS = [
    ("LONG RANGE", "Long Range"), ("PERFORMANCE", "Performance"),
    ("PLAID", "Plaid"), ("STANDARD", "Standard Range"),
]


def extract_car_info(car):
    if not isinstance(car, dict):
        car = {}

    vin = safe(lambda: car.get("VIN") or car.get("vin"), "UNKNOWN")
    price = safe(lambda: car.get("Price"))
    year = safe(lambda: car.get("Year"))
    raw_trim = safe(lambda: car.get("TrimName"))

    def get_exterior():
        names = find_option_descriptions(car, "PAINT")
        return names[0] if names else _first(car.get("PAINT"))
    raw_exterior = safe(get_exterior)

    def get_interior():
        names = find_option_descriptions(car, "INTERIOR_COLORWAY")
        return names[0] if names else _first(car.get("INTERIOR"))
    raw_interior = safe(get_interior)

    def get_autopilot():
        names = [n for n in find_option_descriptions(car, "AUTOPILOT") if isinstance(n, str)]
        if any("Full Self-Driving" in n for n in names):
            return "Full Self-Driving"
        if any("Enhanced Autopilot" in n for n in names):
            return "Enhanced Autopilot"
        if names:
            return "Basic Autopilot"
        return _first(car.get("AUTOPILOT"))
    autopilot = safe(get_autopilot)

    def get_drivetrain():
        parts = [raw_trim]
        for key in ("CATEGORY", "TRIM"):
            val = car.get(key)
            if isinstance(val, list):
                parts.append(" ".join(str(v) for v in val))
        combined = " ".join(p for p in parts if p).upper()
        if "AWD" in combined or "ALL-WHEEL" in combined or "ALL WHEEL" in combined:
            return "AWD"
        if "RWD" in combined or "REAR-WHEEL" in combined or "REAR WHEEL" in combined:
            return "RWD"
        return "Unknown"
    drivetrain = safe(get_drivetrain)

    def get_photo():
        photos = car.get("VehiclePhotos") or []
        for p in photos:
            if isinstance(p, dict) and p.get("pictureType") == "Front Full View" and p.get("imageUrl"):
                return p["imageUrl"]
        if photos and isinstance(photos[0], dict):
            return photos[0].get("imageUrl")
        return None
    photo_url = safe(get_photo, default=None)

    def get_history():
        vh = car.get("VehicleHistory")
        return vh if isinstance(vh, str) and vh else None
    raw_history = safe(get_history)

    damage_disclosure = car.get("DamageDisclosure")
    if not isinstance(damage_disclosure, bool):
        damage_disclosure = None

    return {
        "vin": vin,
        "price": price,
        "year": year,
        "trim": short_label(raw_trim, TRIM_KEYWORDS, fallback=raw_trim),
        "exterior": short_label(raw_exterior, EXTERIOR_KEYWORDS, fallback=raw_exterior),
        "interior": short_label(raw_interior, INTERIOR_KEYWORDS, fallback=raw_interior),
        "autopilot": autopilot,
        "drivetrain": drivetrain,
        "history": raw_history.title() if raw_history else "Unknown",
        "damage_disclosure": damage_disclosure,
        "link": f"https://www.tesla.com/en_AE/order/{vin}?redirect=no",
        "photo_url": photo_url,
    }


# ----------------------------------------------------------- classification

def classify_autopilot(autopilot):
    if autopilot in ("Enhanced Autopilot", "Full Self-Driving"):
        return "✅"
    if autopilot == "Unknown":
        return "❓"
    return "❌"


def classify_history(history, damage_disclosure):
    if history == "Unknown" and damage_disclosure is None:
        return "❓"
    if damage_disclosure is True:
        return "❌"
    if history != "Unknown" and history.upper() != "CLEAN":
        return "❌"
    if history == "Clean" or damage_disclosure is False:
        return "✅"
    return "❓"


def classify_exterior(exterior):
    if exterior in ("Unknown", None):
        return "❓"
    if exterior in ("Red", "Blue"):
        return "❌"
    return "✅"


def classify_interior(interior):
    if interior in ("Unknown", None):
        return "❓"
    if interior == "Black":
        return "❌"
    if interior in ("White", "Cream"):
        return "✅"
    return "❓"


def classify_year(year):
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "❓"
    return "✅" if y >= 2025 else "❌"


def classify_trim(trim):
    if trim in ("Unknown", None):
        return "❓"
    if trim == "Long Range":
        return "✅"
    if trim in ("Performance", "Plaid", "Standard Range"):
        return "❌"
    return "❓"


def classify_drivetrain(drivetrain):
    if drivetrain == "AWD":
        return "⭐"
    if drivetrain == "RWD":
        return "✅"
    return "❓"


def esc(value):
    return html.escape(str(value), quote=False)


def build_message(info, model_name, model_code=None, is_test=False):
    ap_marker = classify_autopilot(info["autopilot"])
    ext_marker = classify_exterior(info["exterior"])
    int_marker = classify_interior(info["interior"])
    year_marker = classify_year(info["year"])
    trim_marker = classify_trim(info["trim"])
    drive_marker = classify_drivetrain(info["drivetrain"])
    history_marker = classify_history(info["history"], info["damage_disclosure"])

    hard = [ap_marker, ext_marker, int_marker, year_marker, trim_marker, history_marker]
    if "❌" in hard:
        verdict = "❌ SKIP: does not match your criteria"
    elif "❓" in hard:
        verdict = "❓ Some details unclear, worth checking manually"
    else:
        verdict = "✅ Matches your criteria"

    is_full_match = verdict == "✅ Matches your criteria"
    header = "🚀🚀🚀🚀🚀\n" if (is_full_match and model_code == "my" and not is_test) else ""

    return (
        f"{header}"
        f"<b>New Tesla {esc(model_name)} CPO</b>\n\n"
        f"<b>{esc(verdict)}</b>\n\n"
        f"{year_marker} Year: <b>{esc(info['year'])}</b>\n"
        f"{trim_marker} Trim: <b>{esc(info['trim'])}</b>\n"
        f"{drive_marker} Drivetrain: <b>{esc(info['drivetrain'])}</b>\n"
        f"{ext_marker} Exterior: <b>{esc(info['exterior'])}</b>\n"
        f"{int_marker} Interior: <b>{esc(info['interior'])}</b>\n"
        f"{ap_marker} Autopilot: <b>{esc(info['autopilot'])}</b>\n"
        f"{history_marker} History: <b>{esc(info['history'])}</b>\n"
        f"💰 Price: <b>AED {esc(info['price'])}</b>\n"
        f"🔗 Link: <b>{esc(info['link'])}</b>"
    )


# ------------------------------------------------------------------ main loop

def check_once(state):
    cars, failed, ok_models = collect_listings()

    if not ok_models:
        state["fail_streak"] += 1
        print(f"Every model fetch failed. Streak: {state['fail_streak']}")
        if state["fail_streak"] == FAIL_ALERT_AFTER:
            send_telegram(
                "<b>⚠️ Tesla watch is blind</b>\n\n"
                f"The last {FAIL_ALERT_AFTER} attempts to read Tesla inventory "
                "all failed. You are not being alerted about new cars right now. "
                "Open the GitHub Actions log to see the HTTP error."
            )
        save_state(state)
        return

    if state["fail_streak"] >= FAIL_ALERT_AFTER:
        send_telegram("<b>✅ Tesla watch recovered</b>\n\nInventory reads are working again.")
    state["fail_streak"] = 0

    was_armed = bool(state["seeded_models"])
    baseline = []
    new_cars = []

    for model_code, model_name, car in cars:
        vin = car.get("VIN") or car.get("vin")
        if not vin or vin in state["seen"]:
            continue
        if model_code in state["seeded_models"]:
            new_cars.append((model_code, model_name, car, vin))
        else:
            baseline.append(vin)

    if baseline:
        state["seen"].update(baseline)
        print(f"Baselined {len(baseline)} existing listings without alerting.")
    state["seeded_models"].update(ok_models)
    save_state(state)

    if not was_armed:
        send_telegram(
            "<b>Tesla watch armed</b>\n\n"
            f"Baseline saved: {len(state['seen'])} listings already live on Tesla UAE.\n"
            "From now on you only get a message when a NEW car appears."
        )
        print(f"Armed with {len(state['seen'])} VINs.")
        return

    if not new_cars:
        print("No new listings.")
        return

    print(f"{len(new_cars)} new listing(s).")

    for i, (model_code, model_name, car, vin) in enumerate(new_cars):
        if i >= MAX_ALERTS_PER_POLL:
            overflow = new_cars[MAX_ALERTS_PER_POLL:]
            state["seen"].update(v for _, _, _, v in overflow)
            save_state(state)
            send_telegram(
                f"<b>{len(overflow)} more new listings</b> were skipped this round "
                "to stay under Telegram's rate limit. "
                "Check https://www.tesla.com/en_AE/inventory/used/my directly."
            )
            break

        if NOTIFY_ONLY_MODEL is not None and model_code != NOTIFY_ONLY_MODEL:
            state["seen"].add(vin)
            save_state(state)
            print(f"[{model_name}] {vin} skipped, filtered to {NOTIFY_ONLY_MODEL}")
            continue

        try:
            info = extract_car_info(car)
            msg = build_message(info, model_name, model_code=model_code)
            delivered = send_telegram(msg, info["photo_url"])
        except Exception as e:
            print(f"[{model_name}] could not parse {vin}: {e}")
            delivered = send_telegram(
                f"<b>New Tesla {esc(model_name)} CPO</b>\n\n"
                "Details could not be read, open it directly:\n"
                f"https://www.tesla.com/en_AE/order/{esc(vin)}?redirect=no"
            )

        if delivered:
            state["seen"].add(vin)
            save_state(state)   # written immediately, survives a cancelled job
            print(f"[{model_name}] notified {vin}")
        else:
            print(f"[{model_name}] send failed for {vin}, will retry next poll")

        time.sleep(SECONDS_BETWEEN_ALERTS)


def send_test_message():
    mock_car = {
        "VIN": "5YJYGDEE1PF000123",
        "Price": 145000,
        "Year": 2022,
        "TrimName": "Long Range AWD",
        "CATEGORY": ["MYLRAWD"],
        "PAINT": ["WHITE"],
        "INTERIOR": ["BLACK"],
        "AUTOPILOT": ["AUTOPILOT"],
        "VehicleHistory": "CLEAN",
        "DamageDisclosure": False,
        "OptionCodeData": [
            {"group": "PAINT", "description": "Pearl White Multi-Coat"},
            {"group": "INTERIOR_COLORWAY", "description": "All Black Premium Interior"},
            {"group": "AUTOPILOT", "description": "Enhanced Autopilot"},
        ],
        "VehiclePhotos": [],
    }
    info = extract_car_info(mock_car)
    msg = "<b>[TEST MESSAGE, this is not a real listing]</b>\n\n" + build_message(
        info, "Model Y", model_code="my", is_test=True
    )
    ok = send_telegram(msg, info["photo_url"])
    print(f"Test message delivered: {ok}")
    if not ok:
        raise SystemExit("Telegram test send failed, check the secrets.")


def main():
    if TEST_MESSAGE:
        send_test_message()
        return

    state = load_state()
    start = time.time()
    while True:
        try:
            check_once(state)
        except Exception as e:
            # Never lose the run's progress to an unexpected error.
            print(f"Unexpected error in poll: {e}")
            save_state(state)
        if time.time() - start >= LOOP_SECONDS:
            break
        time.sleep(POLL_EVERY_SECONDS)

    save_state(state)
    print("Run complete.")


if __name__ == "__main__":
    main()
