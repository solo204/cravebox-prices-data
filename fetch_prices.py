"""
Fetch real retail prices from Kroger API for all ingredients in prices_us.json.
Uses client_credentials OAuth2 flow (no user login required).
"""

import json
import os
import time
import requests
from base64 import b64encode

CLIENT_ID = os.environ["KROGER_CLIENT_ID"]
CLIENT_SECRET = os.environ["KROGER_CLIENT_SECRET"]
BASE_URL = "https://api.kroger.com/v1"

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token():
    creds = b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        f"{BASE_URL}/connect/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=product.compact",
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ── Store lookup (required for price data) ────────────────────────────────────

def get_store_id(token):
    """Pick a large Kroger store in a mid-size US city for representative prices."""
    r = requests.get(
        f"{BASE_URL}/locations",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "filter.zipCode.near": "43215",   # Columbus, OH — Kroger HQ area
            "filter.limit": 1,
            "filter.chain": "KROGER",
        },
    )
    r.raise_for_status()
    locations = r.json().get("data", [])
    if not locations:
        raise RuntimeError("No Kroger locations found")
    store_id = locations[0]["locationId"]
    print(f"Using store: {locations[0].get('name', store_id)} ({store_id})")
    return store_id


# ── Price lookup ──────────────────────────────────────────────────────────────

def search_product(token, store_id, ingredient_name):
    """Search for a product and return the best price match."""
    r = requests.get(
        f"{BASE_URL}/products",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "filter.term": ingredient_name,
            "filter.locationId": store_id,
            "filter.limit": 5,
            "filter.fulfillment": "ais",   # in-store
        },
    )
    if r.status_code == 429:
        print(f"  Rate limited — sleeping 60s")
        time.sleep(60)
        return search_product(token, store_id, ingredient_name)
    if r.status_code != 200:
        return None

    products = r.json().get("data", [])
    if not products:
        return None

    # Pick first product that has a price
    for product in products:
        items = product.get("items", [])
        for item in items:
            price_info = item.get("price", {})
            regular = price_info.get("regular")
            size = item.get("size", "")
            if regular and regular > 0:
                return {
                    "price": regular,
                    "size": size,
                    "description": product.get("description", ""),
                }
    return None


def parse_unit_and_price(match, ingredient):
    """
    Normalize Kroger price to match our priceUnit schema.
    Kroger returns price per package; we convert to price per lb/each/dozen.
    """
    price = match["price"]
    size = match["size"].lower().strip()
    price_unit = ingredient["priceUnit"]

    # For "each" items (canned goods, bottles, loaves) — use package price directly
    if price_unit in ("each", "dozen"):
        return price, price_unit

    # For lb-priced items, try to extract weight from size string and normalize
    import re
    oz_match = re.search(r"(\d+(?:\.\d+)?)\s*oz", size)
    lb_match = re.search(r"(\d+(?:\.\d+)?)\s*lb", size)

    if lb_match:
        pkg_lbs = float(lb_match.group(1))
        return round(price / pkg_lbs, 2), "lb"
    elif oz_match:
        pkg_oz = float(oz_match.group(1))
        pkg_lbs = pkg_oz / 16.0
        if pkg_lbs > 0:
            return round(price / pkg_lbs, 2), "lb"

    # Fallback: return raw price, keep existing unit
    return price, price_unit


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open("prices_us.json") as f:
        ingredients = json.load(f)

    print(f"Loaded {len(ingredients)} ingredients")

    token = get_token()
    store_id = get_store_id(token)

    updated = 0
    failed = 0

    for i, ingredient in enumerate(ingredients):
        name = ingredient["name"]
        print(f"[{i+1}/{len(ingredients)}] {name}...", end=" ")

        try:
            match = search_product(token, store_id, name)
            if match:
                price, unit = parse_unit_and_price(match, ingredient)
                if price and 0.01 < price < 500:   # sanity check
                    ingredient["priceUsd"] = price
                    ingredient["priceUnit"] = unit
                    ingredient["source"] = "kroger"
                    ingredient["updatedAt"] = int(time.time() * 1000)
                    print(f"${price:.2f}/{unit} ({match['description'][:40]})")
                    updated += 1
                else:
                    print(f"skipped (price out of range: ${price})")
                    failed += 1
            else:
                print("no match")
                failed += 1
        except Exception as e:
            print(f"error: {e}")
            failed += 1

        # Polite delay — stay well under 1600/day limit
        time.sleep(0.5)

    with open("prices_us.json", "w") as f:
        json.dump(ingredients, f, indent=2)

    print(f"\nDone: {updated} updated, {failed} not matched")


if __name__ == "__main__":
    main()
