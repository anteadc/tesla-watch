import json
import os
import time
import requests

STATE_FILE = "seen_vins.json"
LOOP_SECONDS = 270      # keep this under 300 (5 min) so it finishes before the next trigger
POLL_EVERY_SECONDS = 30
URL = "https://www.tesla.com/inventory/api/v1/inventory-results"

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ---------------------------------------------------------------------------
# VERIFY THIS BLOCK before relying on it. Open tesla.com/en_AE/inventory/used/my
# in a browser, open dev tools > Network tab, filter "inventory-results", and
# compare the "query" param it sends against what's below. market/super_region
# are the two fields most likely to be wrong here, I could not test-fetch
# tesla.com from this environment to confirm them.
# ---------------------------------------------------------------------------
QUERY = {
    "query": {
        "model": "my",
        "condition": "used",
        "options": {},
        "arrangeby": "Price",
        "order": "asc",
        "market": "AE",
        "language": "en",
        "super_region": "other",
        "lng": 55.2708,   # Dubai
        "lat": 25.2048,
        "zip": "",
        "range": 0,
    },
    "offset": 0,
    "count": 50,
    "outsideOffset": 0,
    "outsideSearch": False,
}


def fetch_inventory():
    resp = requests.get(
        URL,
        params={"query": json.dumps(QUERY)},
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


def format_car(car):
    # Field names are best-guess from community-documented Tesla API responses.
    # On first run, this prints the raw object too, so you can fix field names
    # if Tesla's actual keys differ.
    vin = car.get("VIN") or car.get("vin", "UNKNOWN")
    price = car.get("Price", "?")
    year = car.get("Year", "")
    trim = car.get("TrimName", car.get("Trim", ""))
    paint = car.get("PAINT")
    color = paint[0] if isinstance(paint, list) and paint else (paint or "")
    link = f"https://www.tesla.com/en_AE/my/order/{vin}?redirect=no"
    return (
        f"New Tesla Model Y CPO\n"
        f"{year} {trim} {color}\n"
        f"Price: AED {price}\n"
        f"{link}"
    ), vin


def check_once(seen):
    data = fetch_inventory()
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
        # Very first check ever: print one raw record so field names can be checked.
        print("First check, raw sample record for field-name verification:")
        print(json.dumps(results[0], indent=2))

    for car in new_cars:
        msg, vin = format_car(car)
        send_telegram(msg)
        print(f"Notified: {vin}")

    if not new_cars:
        print(f"No new listings. {len(results)} total in current search.")

    return new_seen


def main():
    # GitHub can only trigger this job once every 5 minutes at best.
    # To get closer to 30-second checks, each job run stays alive for
    # ~4.5 minutes and polls internally every 30 seconds, then exits
    # just before the next scheduled trigger is due.
    seen = load_seen()
    start = time.time()
    while time.time() - start < LOOP_SECONDS:
        try:
            seen = check_once(seen)
        except Exception as e:
            print(f"Check failed, will retry next loop: {e}")
        time.sleep(POLL_EVERY_SECONDS)
    save_seen(seen)


if __name__ == "__main__":
    main()
