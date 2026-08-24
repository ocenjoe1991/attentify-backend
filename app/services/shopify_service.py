import re
import asyncio
import urllib.parse
import logging
import os
import requests
from datetime import datetime, timezone
from pymongo import UpdateOne
from bson import ObjectId

logger = logging.getLogger("attentify.shopify_service")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")


def _to_datetime(value):
    """Convert a Shopify ISO 8601 string to a timezone-aware datetime.
    Returns None if parsing fails (TTL index won't apply, order still stored)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        logger.debug("Failed to parse date: %s", value)
        return None

async def get_all_shopify_creds(db):
    """Fetch all Shopify store credentials from the database."""
    # Use .to_list() with a reasonable length
    return await db.shopify_cred.find({"status": "connected"}).to_list(length=100)


def fetch_access_scopes(shop, access_token):
    """Return the scopes granted to the current Shopify access token."""
    url = f"https://{shop}/admin/oauth/access_scopes.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except Exception as exc:
        logger.warning("Failed to fetch Shopify access scopes for %s: %s", shop, exc)
        return []

    if resp.status_code != 200:
        logger.warning(
            "Failed to fetch Shopify access scopes for %s: HTTP %d %s",
            shop,
            resp.status_code,
            resp.text[:300],
        )
        return []

    scopes = [
        item.get("handle")
        for item in resp.json().get("access_scopes", [])
        if item.get("handle")
    ]
    logger.info("Shopify access scopes for %s: %s", shop, ",".join(scopes) or "(none)")
    return scopes


async def fetch_order_updated_at_from_shop(shop, access_token, order_id):
    """Fetch only the updated_at field for one Shopify order."""
    url = (
        f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}.json"
        "?fields=id,updated_at"
    )
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=20)
    if resp.status_code != 200:
        logger.warning(
            "Shopify single-order updated_at check failed for %s/%s: HTTP %d",
            shop,
            order_id,
            resp.status_code,
        )
        return None
    return _to_datetime((resp.json().get("order") or {}).get("updated_at"))


async def fetch_order_from_shop(shop, access_token, order_id):
    """Fetch one full Shopify order."""
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Shopify single-order fetch failed for {shop}/{order_id}: HTTP {resp.status_code}")
    return resp.json().get("order")

# Fetch full orders from a shopify store
def fetch_orders_from_shop1(shop, access_token):
    """Fetch all orders from a Shopify store using the access token."""
    orders = []
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json?status=any&limit=250"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    page_info = None
    while True:
        req_url = url
        if page_info:
            req_url += f"&page_info={page_info}"
        resp = requests.get(req_url, headers=headers)
        if resp.status_code != 200:
            break
        data = resp.json()
        if "orders" not in data:
            break
        orders.extend(data["orders"])
        # Pagination (Shopify uses 'link' header for next page)
        link = resp.headers.get("link")
        if link and 'rel="next"' in link:
            import re
            match = re.search(r'<([^>]+)>; rel="next"', link)
            if match:
                next_url = match.group(1)
                import urllib.parse
                page_info = urllib.parse.parse_qs(urllib.parse.urlparse(next_url).query).get("page_info", [None])[0]
                if not page_info:
                    break
            else:
                break
        else:
            break
    return orders

async def fetch_orders_from_shop(shop, access_token, updated_at_min=None):
    """Fetch orders from a Shopify store using cursor pagination.
    If updated_at_min is provided, only orders updated after that time are fetched.
    Otherwise, sets created_at_min far in the past to bypass Shopify's 60-day default limit."""
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json?limit=250&status=any"
    if updated_at_min:
        url += f"&updated_at_min={urllib.parse.quote(updated_at_min)}"
    else:
        # First sync: bypass Shopify's 60-day default by setting a very old created_at_min
        url += "&created_at_min=" + urllib.parse.quote("2020-01-01T00:00:00Z")
    url += "&order=created_at desc"
    logger.info("Fetching orders from Shopify: %s", url)
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    orders = []
    next_url = url
    page_count = 0

    while next_url:
        page_count += 1
        resp = await asyncio.to_thread(
            requests.get,
            next_url,
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("Shopify fetch page %d failed: HTTP %d for %s", page_count, resp.status_code, shop)
            raise RuntimeError(f"Shopify fetch failed for {shop}: HTTP {resp.status_code}")

        data = resp.json()
        page_orders = data.get("orders", [])
        orders.extend(page_orders)
        logger.info("Shopify fetch page %d: got %d orders (total so far: %d) for %s", page_count, len(page_orders), len(orders), shop)

        link = resp.headers.get("link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        next_url = match.group(1) if match else None
        if not next_url:
            logger.info(
                "Shopify fetch page %d has no next page for %s (link header: %s)",
                page_count,
                shop,
                link or "empty",
            )

        if next_url:
            parsed = urllib.parse.urlparse(next_url)
            query = urllib.parse.parse_qs(parsed.query)
            page_info = query.get("page_info", [None])[0]
            next_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json?limit=250&page_info={page_info}" if page_info else None
        
        if not next_url:
            logger.info("Shopify fetch complete: %d total orders, %d pages for %s", len(orders), page_count, shop)

    return orders


async def fetch_order_pages_from_shop(shop, access_token, updated_at_min=None):
    """Yield Shopify orders one page at a time using cursor pagination."""
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json?limit=250&status=any"
    if updated_at_min:
        url += f"&updated_at_min={urllib.parse.quote(updated_at_min)}"
    else:
        url += "&created_at_min=" + urllib.parse.quote("2020-01-01T00:00:00Z")
    url += "&order=created_at desc"
    logger.info("Fetching orders from Shopify in pages: %s", url)

    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    next_url = url
    page_count = 0
    total_count = 0

    while next_url:
        page_count += 1
        resp = await asyncio.to_thread(
            requests.get,
            next_url,
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("Shopify fetch page %d failed: HTTP %d for %s", page_count, resp.status_code, shop)
            raise RuntimeError(f"Shopify fetch failed for {shop}: HTTP {resp.status_code}")

        page_orders = resp.json().get("orders", [])
        total_count += len(page_orders)

        link = resp.headers.get("link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        raw_next_url = match.group(1) if match else None
        if raw_next_url:
            parsed = urllib.parse.urlparse(raw_next_url)
            query = urllib.parse.parse_qs(parsed.query)
            page_info = query.get("page_info", [None])[0]
            next_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json?limit=250&page_info={page_info}" if page_info else None
        else:
            next_url = None

        logger.info(
            "Shopify fetch page %d: got %d orders (total so far: %d) for %s",
            page_count,
            len(page_orders),
            total_count,
            shop,
        )
        yield {
            "orders": page_orders,
            "page": page_count,
            "total": total_count,
            "has_next": bool(next_url),
        }

    logger.info("Shopify fetch complete: %d total orders, %d pages for %s", total_count, page_count, shop)

async def upsert_orders(db, shop, orders):
    """Insert or update orders in the database for a specific shop."""

    user_id, company_id = None, None

    cred = await db.shopify_cred.find_one({"shop": shop})
    if not cred:
        logger.warning("Shopify credentials not found for shop: %s", shop)
        user_id = None
        company_id = None
    else:
        user_id = cred.get("user_id")
        company_id = cred.get("company_id")

    bulk_ops = []
    for order in orders:
        customer = order.get("customer") or {}
        shipping_address = order.get("shipping_address") or {}
        billing_address = order.get("billing_address") or {}
        default_address = customer.get("default_address") or shipping_address or billing_address or {}
        customer_email = (
            customer.get("email")
            or order.get("email")
            or order.get("contact_email")
            or shipping_address.get("email")
            or billing_address.get("email")
        )
        customer_phone = (
            customer.get("phone")
            or order.get("phone")
            or shipping_address.get("phone")
            or billing_address.get("phone")
        )
        first_name = customer.get("first_name") or shipping_address.get("first_name") or billing_address.get("first_name") or ""
        last_name = customer.get("last_name") or shipping_address.get("last_name") or billing_address.get("last_name") or ""
        customer_name = f"{first_name} {last_name}".strip() or order.get("customer_locale", "")
        doc = {
            "order_id": order["id"],
            "user_id": ObjectId(user_id),
            "company_id": ObjectId(company_id),
            "order_number": order.get("order_number"),
            "name": order.get("name"),
            "shop": shop,
            "created_at": _to_datetime(order.get("created_at")),
            "customer": {
                "id": customer.get("id"),
                "email": customer_email,
                "name": customer_name,
                "phone": customer_phone,
                "default_address": {
                    "address1": default_address.get("address1"),
                    "address2": default_address.get("address2"),
                    "city": default_address.get("city"),
                    "province": default_address.get("province"),
                    "country": default_address.get("country"),
                    "zip": default_address.get("zip"),
                }
            },
            "shipping_address": shipping_address,
            "billing_address": billing_address,
            "total_shipping_price": (
                order.get("total_shipping_price_set", {})
                    .get("shop_money", {})
                    .get("amount", 0)
            ),
            "total_price": order.get("total_price"),
            "payment_status": order.get("financial_status"),
            "fulfillment_status": order.get("fulfillment_status"),
            "cancelled_at": order.get("cancelled_at"),
            "cancel_reason": order.get("cancel_reason"),
            "closed_at": order.get("closed_at"),
            "refunds": order.get("refunds", []),
            "fulfillments": order.get("fulfillments", []),
            "line_items": [
                {
                    "id": item.get("id"),
                    "product_id": item.get("product_id"),
                    "name": item.get("name"),
                    "quantity": item.get("quantity"),
                    "price": item.get("price"),
                }
                for item in order.get("line_items", [])
            ],
            "updated_at": order.get("updated_at")
        }
        bulk_ops.append(
            UpdateOne(
                {
                    "company_id": doc["company_id"],
                    "order_id": doc["order_id"],
                    "shop": shop,
                },
                {"$set": doc},
                upsert=True
            )
        )
    if bulk_ops:
        await db.orders.bulk_write(bulk_ops)


async def sync_company_orders_incremental(db, company_id, *, source: str = "manual") -> dict:
    """Sync connected Shopify stores before workflows that depend on fresh order data."""
    company_object_id = company_id if isinstance(company_id, ObjectId) else ObjectId(company_id)
    now = datetime.now(timezone.utc)
    cursor = db["shopify_cred"].find({
        "company_id": company_object_id,
        "status": "connected",
        "access_token": {"$exists": True, "$ne": ""},
    })
    synced_shops = 0
    total_synced = 0
    errors = []

    async for cred in cursor:
        shop = cred.get("shop")
        access_token = cred.get("access_token")
        if not shop or not access_token:
            continue

        try:
            scopes = await asyncio.to_thread(fetch_access_scopes, shop, access_token)
            if "read_all_orders" not in scopes:
                logger.warning(
                    "Shopify token for %s does not include read_all_orders; historical orders may be limited to recent orders.",
                    shop,
                )

            last_synced = cred.get("last_synced_at")
            updated_at_min = last_synced.isoformat() if last_synced else None
            shop_synced = 0
            async for page in fetch_order_pages_from_shop(shop, access_token, updated_at_min):
                await upsert_orders(db, shop, page["orders"])
                shop_synced = page["total"]

            await db["shopify_cred"].update_one(
                {"_id": cred["_id"]},
                {"$set": {
                    "last_synced_at": now,
                    "last_checked_scopes": scopes,
                    "has_read_all_orders": "read_all_orders" in scopes,
                }},
            )
            synced_shops += 1
            total_synced += shop_synced
            logger.info(
                "Synced %d orders for shop %s before %s Gmail sync (since %s)",
                shop_synced,
                shop,
                source,
                updated_at_min or "beginning",
            )
        except Exception as exc:
            logger.error("Error syncing %s before %s Gmail sync: %s", shop, source, exc)
            errors.append({"shop": shop, "error": str(exc)})

    if synced_shops == 0:
        logger.warning("No connected Shopify stores with access tokens found for company %s", company_object_id)

    return {
        "synced_shops": synced_shops,
        "synced_orders": total_synced,
        "errors": errors,
    }
