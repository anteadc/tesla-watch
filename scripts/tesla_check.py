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

# Tesla's API takes one model per request, so checking "everything" means
# one request per model. Codes confirmed from tesla.com/en_AE/inventory/used/<code>
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
            "options": {"Year": [0]},
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
    """Some Tesla fields come back as a list with one item, some as a plain
    string. Normalize either into a single value, or None."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def extract_car_info(car):
    # NOT YET CONFIRMED against a real Tesla API response, these are the
    # field names used in community-documented Tesla API code. Exact keys
    # (especially INTERIOR, AUTOPILOT, and any photo field) may differ.
    # Once a real check prints a raw record to the Actions log, compare it
    # against this function and fix any mismatch.
    vin = car.get("VIN") or car.get("vin") or "UNKNOWN"
    price = car.get("Price", "?")
    year = car.get("Year", "")
    trim = car.get("TrimName", car.get("Trim", ""))

    exterior = _first(car.get("PAINT")) or "Unknown"

    interior = (
        _first(car.get("INTERIOR"))
        or _first(car.get("INTERIOR_DECOR"))
        or _first(car.get("Interior"))
        or "Unknown"
    )

    autopilot_raw = _first(car.get("AUTOPILOT")) or _first(car.get("Autopilot"))
    if autopilot_raw:
        ap = str(autopilot_raw).upper()
        if "FULL_SELF_DRIVING" in ap:
            autopilot = "Full Self-Driving"
        elif "ENHANCED" in ap:
            autopilot = "Enhanced Autopilot"
        elif "AUTOPILOT" in ap or "BASE" in ap:
            autopilot = "Basic Autopilot"
        else:
            autopilot = str(autopilot_raw)
    else:
        autopilot = "Unknown"

    # Photo: Tesla is reported (per community trackers) to have stopped
    # exposing per-vehicle photos in this API in recent years. These key
    # names are a guess at what might still work. If none match, no photo
    # is sent, which is fine, just text.
    photo_url = None
    for key in ("CompositorImageURL", "compositorURL", "IMAGE_URL", "Thumbnail", "thumbnail"):
        val = _first(car.get(key))
        if val:
            photo_url = val
            break

    link = f"https://www.tesla.com/en_AE/order/{vin}?redirect=no"

    return {
        "vin": vin,
        "price": price,
        "year": year,
        "trim": trim,
        "exterior": exterior,
        "interior": interior,
        "autopilot": autopilot,
        "link": link,
        "photo_url": photo_url,
    }


def build_message(info, model_name):
    return (
        f"New Tesla {model_name} CPO\n\n"
        f"Year: {info['year']}\n"
        f"Trim: {info['trim']}\n"
        f"Exterior: {info['exterior']}\n"
        f"Interior: {info['interior']}\n"
        f"Autopilot: {info['autopilot']}\n"
        f"Price: AED {info['price']}\n"
        f"Link: {info['link']}"
    )


def send_telegram(text, photo_url=None):
    if photo_url:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url, "caption": text},
            timeout=15,
        )
        if resp.ok and resp.json().get("ok"):
            return
        print("Photo send failed, falling back to text-only message.")

    # No photo URL, or the photo send failed: send plain text, and turn off
    # Telegram's automatic link preview so it doesn't guess a wrong photo.
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )


def check_model(model_code, model_name, seen):
    data = fetch_inventory(model_code)
    results = data.get("results", [])
    new_seen = set(seen)
    new_cars = []

    for car in results:
        vin = car.get("VIN") or car.get("vin")
        if not vin:
            continue
        new_seen.add(vin)
        if vin not in seen:
            new_cars.append(car)

    if not seen and results:
        print(f"[{model_name}] First check, raw sample record (use this to fix field names above):")
        print(json.dumps(results[0], indent=2))

    for car in new_cars:
        info = extract_car_info(car)
        should_notify = NOTIFY_ONLY_MODEL is None or model_code == NOTIFY_ONLY_MODEL
        if should_notify:
            msg = build_message(info, model_name)
            send_telegram(msg, info["photo_url"])
            print(f"[{model_name}] Notified: {info['vin']}")
        else:
            print(f"[{model_name}] New listing found (not notified, filtered to {NOTIFY_ONLY_MODEL}): {info['vin']}")

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
        "PAINT": ["Pearl White"],
        "INTERIOR": ["Black"],
        "AUTOPILOT": ["TESLA_AUTOPILOT_ENHANCED_AUTOPILOT"],
        # No photo field on purpose, this tests the no-photo fallback path,
        # since real photos may not be available at all (see note above).
    }
    info = extract_car_info(mock_car)
    msg = "[TEST MESSAGE, this is not a real listing]\n\n" + build_message(info, "Model Y")
    send_telegram(msg, info["photo_url"])
    print(f"Sent mock test message, VIN {info['vin']}")


def main():
    if TEST_MESSAGE:
        send_test_message()
        return

    # GitHub can only trigger this job once every 5 minutes at best.
    # To get closer to 30-second checks, each job run stays alive for
    # ~4.5 minutes and polls internally every 30 seconds, then exits
    # just before the next scheduled trigger is due.
    seen = load_seen()
    start = time.time()
    while time.time() - start < LOOP_SECONDS:
        seen = check_once(seen)
        time.sleep(POLL_EVERY_SECONDS)
    save_seen(seen)


if __name__ == "__main__":
    main()
