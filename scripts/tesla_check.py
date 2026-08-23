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
    """Run fn(), return its result, or default if anything at all goes
    wrong or the result is empty. Nothing in here can crash the script."""
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


def extract_car_info(car):
    if not isinstance(car, dict):
        car = {}

    vin = safe(lambda: car.get("VIN") or car.get("vin"), "UNKNOWN")
    price = safe(lambda: car.get("Price"))
    year = safe(lambda: car.get("Year"))
    trim = safe(lambda: car.get("TrimName"))

    def get_exterior():
        names = find_option_descriptions(car, "PAINT")
        return names[0] if names else _first(car.get("PAINT"))
    exterior = safe(get_exterior)

    def get_interior():
        names = find_option_descriptions(car, "INTERIOR_COLORWAY")
        return names[0] if names else _first(car.get("INTERIOR"))
    interior = safe(get_interior)

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
    try:
        if photo_url:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url, "caption": text},
                timeout=15,
            )
            if resp.ok and resp.json().get("ok"):
                return
            print("Photo send failed, falling back to text-only message.")

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
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
                msg = build_message(info, model_name)
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
    msg = "[TEST MESSAGE, this is not a real listing]\n\n" + build_message(info, "Model Y")
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
