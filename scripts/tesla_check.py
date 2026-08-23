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

# Tesla's API takes one model per request, so checking "everything" means
# one request per model. Codes confirmed from tesla.com/en_AE/inventory/used/<code>
MODELS = {
    "my": "Model Y",
    "m3": "Model 3",
    "ms": "Model S",
    "mx": "Model X",
}

# Set this to "my" once you've confirmed the pipeline works and only want
# Model Y alerts. Leave as None to get alerts for ANY model, useful right
# now since Model Y CPO stock is rare and you want to see a real alert
# fire soon to prove the whole chain works end to end.
NOTIFY_ONLY_MODEL = None   # None = alert on any model. Change to "my" later.


def build_query(model_code):
    # Confirmed working: captured directly from tesla.com/en_AE/inventory/used/my
    # via browser dev tools, returned 200 OK. No lat/lng/zip needed for this
    # market. super_region is "north america" even though this is the UAE
    # site, that's just what Tesla's backend expects here.
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


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )


def format_car(car, model_name):
    # Field names are best-guess from community-documented Tesla API responses,
    # not yet confirmed against a live UAE record. Each model's first-ever
    # check prints one raw record so you can fix field names if needed.
    vin = car.get("VIN") or car.get("vin", "UNKNOWN")
    price = car.get("Price", "?")
    year = car.get("Year", "")
    trim = car.get("TrimName", car.get("Trim", ""))
    paint = car.get("PAINT")
    color = paint[0] if isinstance(paint, list) and paint else (paint or "")
    link = f"https://www.tesla.com/en_AE/order/{vin}?redirect=no"
    return (
        f"New Tesla {model_name} CPO\n"
        f"{year} {trim} {color}\n"
        f"Price: AED {price}\n"
        f"{link}"
    ), vin


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
        print(f"[{model_name}] First check, raw sample record:")
        print(json.dumps(results[0], indent=2))

    for car in new_cars:
        msg, vin = format_car(car, model_name)
        should_notify = NOTIFY_ONLY_MODEL is None or model_code == NOTIFY_ONLY_MODEL
        if should_notify:
            send_telegram(msg)
            print(f"[{model_name}] Notified: {vin}")
        else:
            print(f"[{model_name}] New listing found (not notified, filtered to {NOTIFY_ONLY_MODEL}): {vin}")

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


def main():
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
