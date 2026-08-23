import html
import json
import os
import time
import requests

STATE_FILE = "seen_vins.json"
LOOP_SECONDS = 270      # keep this under 300 (5 min) so it finishes before the next trigger
POLL_EVERY_SECONDS = 30
URL = "https://www.tesla.com/inventory/api/v4/inventory-results"

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TEST_MESSAGE = os.environ.get("TEST_MESSAGE", "false").lower() == "true"

MODELS = {
    "my": "Model Y",
    "m3": "Model 3",
    "ms": "Model S",
    "mx": "Model X",
}

# Set this to "my" once you've confirmed the pipeline works and only want
# Model Y alerts. Leave as None to get alerts for ANY model.
NOTIFY_ONLY_MODEL = None   # None = alert on any model. Change to "my" later.


def build_query(model_code):
    return {
        "query": {
            "model": model_code,
            "condition": "used",
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            "market": "AE",
            "language": "en",
            "super_region": "north america",
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
    resp = requests.get(
        URL,
        params={"query": json.dumps(build_query(model_code))},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE) as f:
        return set(json.load(f))


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


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
    """Reduce a long descriptive string to a short scan-friendly word, e.g.
    'Pearl White Multi-Coat' -> 'White'. Falls back to the original text if
    nothing in keyword_map matches."""
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

    link = f"https://www.tesla.com/en_AE/order/{vin}?redirect=no"

    return {
        "vin": vin,
        "price": price,
        "year": year,
        "trim": short_label(raw_trim, TRIM_KEYWORDS, fallback=raw_trim),
        "exterior": short_label(raw_exterior, EXTERIOR_KEYWORDS, fallback=raw_exterior),
        "interior": short_label(raw_interior, INTERIOR_KEYWORDS, fallback=raw_interior),
        "autopilot": autopilot,
        "drivetrain": drivetrain,
        "link": link,
        "photo_url": photo_url,
    }


def classify_autopilot(autopilot):
    if autopilot in ("Enhanced Autopilot", "Full Self-Driving"):
        return "✅"
    if autopilot == "Unknown":
        return "❓"
    return "❌"


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
    # NOT fully confirmed: assumes Tesla writes "Long Range" in the Model Y
    # trim name. Tell me the real TrimName text once you see one if this
    # needs correcting.
    if trim in ("Unknown", None):
        return "❓"
    if trim == "Long Range":
        return "✅"
    if trim in ("Performance", "Plaid", "Standard Range"):
        return "❌"
    return "❓"  # a trim word we don't recognize, not confident either way


def classify_drivetrain(drivetrain):
    # Preference only, this never causes a skip.
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
    drive_marker = classify_drivetrain(info["drivetrain"])  # excluded from verdict on purpose

    hard_markers = [ap_marker, ext_marker, int_marker, year_marker, trim_marker]
    if "❌" in hard_markers:
        verdict = "❌ SKIP: does not match your criteria"
    elif "❓" in hard_markers:
        verdict = "❓ Some details unclear, worth checking manually"
    else:
        verdict = "✅ Matches your criteria"

    # Rocket header: only for a real (non-test) Model Y that passes every
    # hard filter, this is the "drop everything and go order it" signal,
    # visible in the phone notification preview before you even open it.
    is_full_match = verdict == "✅ Matches your criteria"
    is_model_y = model_code == "my"
    header = "🚀🚀🚀🚀🚀\n" if (is_full_match and is_model_y and not is_test) else ""

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
        f"💰 Price: <b>AED {esc(info['price'])}</b>\n"
        f"🔗 Link: <b>{esc(info['link'])}</b>"
    )


def send_telegram(text, photo_url=None):
    try:
        if photo_url:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": photo_url,
                    "caption": text,
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
            if resp.ok and resp.json().get("ok"):
                return
            print("Photo send failed, falling back to text-only message.")

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram send failed, will not crash the run: {e}")


def check_model(model_code, model_name, seen):
    data = fetch_inventory(model_code)
    results = data.get("results", []) if isinstance(data, dict) else []
    new_seen = set(seen)
    new_cars = []

    for car in results:
        if not isinstance(car, dict):
            continue
        vin = car.get("VIN") or car.get("vin")
        if not vin:
            continue
        new_seen.add(vin)
        if vin not in seen:
            new_cars.append(car)

    if not seen and results:
        print(f"[{model_name}] First check, raw sample record:")
        print(json.dumps(results[0], indent=2))

    for car in new_cars:
        try:
            info = extract_car_info(car)
            should_notify = NOTIFY_ONLY_MODEL is None or model_code == NOTIFY_ONLY_MODEL
            if should_notify:
                msg = build_message(info, model_name, model_code=model_code, is_test=False)
                send_telegram(msg, info["photo_url"])
                print(f"[{model_name}] Notified: {info['vin']}")
            else:
                print(f"[{model_name}] New listing found (not notified, filtered to {NOTIFY_ONLY_MODEL}): {info['vin']}")
        except Exception as e:
            vin_guess = car.get("VIN", "unknown VIN") if isinstance(car, dict) else "unknown VIN"
            print(f"[{model_name}] Failed to process car {vin_guess}, skipped, moving on: {e}")

    if not new_cars:
        print(f"[{model_name}] No new listings. {len(results)} total in current search.")

    return new_seen


def check_once(seen):
    for model_code, model_name in MODELS.items():
        try:
            seen = check_model(model_code, model_name, seen)
        except Exception as e:
            print(f"[{model_name}] Check failed, will retry next loop: {e}")
    return seen


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
        "OptionCodeData": [
            {"group": "PAINT", "description": "Pearl White Multi-Coat"},
            {"group": "INTERIOR_COLORWAY", "description": "All Black Premium Interior"},
            {"group": "AUTOPILOT", "description": "Enhanced Autopilot"},
        ],
        "VehiclePhotos": [],
    }
    info = extract_car_info(mock_car)
    msg = "<b>[TEST MESSAGE, this is not a real listing]</b>\n\n" + build_message(info, "Model Y", model_code="my", is_test=True)
    send_telegram(msg, info["photo_url"])
    print(f"Sent mock test message, VIN {info['vin']}")


def main():
    if TEST_MESSAGE:
        send_test_message()
        return

    seen = load_seen()
    start = time.time()
    while time.time() - start < LOOP_SECONDS:
        seen = check_once(seen)
        time.sleep(POLL_EVERY_SECONDS)
    save_seen(seen)


if __name__ == "__main__":
    main()
