import re
import urllib.parse
import logging
import requests
from datetime import datetime, timezone
from pymongo import UpdateOne
from bson import ObjectId

logger = logging.getLogger("attentify.shopify_service")


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
    return await db.shopify_cred.find({}).to_list(length=100)

# Fetch full orders from a shopify store
def fetch_orders_from_shop1(shop, access_token):
    """Fetch all orders from a Shopify store using the access token."""
    orders = []
    url = f"https://{shop}/admin/api/2025-10/orders.json?status=any&limit=250"
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
    If updated_at_min is provided (ISO 8601), only orders updated after that time are fetched."""
    url = f"https://{shop}/admin/api/2025-10/orders.json?status=any&limit=250"
    if updated_at_min:
        url += f"&updated_at_min={updated_at_min}"
    url += "&order=created_at desc"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    orders = []
    next_url = url
    page_count = 0

    while next_url:
        page_count += 1
        resp = requests.get(next_url, headers=headers)
        if resp.status_code != 200:
            logger.warning("Shopify fetch page %d failed: HTTP %d for %s", page_count, resp.status_code, shop)
            break

        data = resp.json()
        page_orders = data.get("orders", [])
        orders.extend(page_orders)
        logger.info("Shopify fetch page %d: got %d orders (total so far: %d) for %s", page_count, len(page_orders), len(orders), shop)

        link = resp.headers.get("link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        next_url = match.group(1) if match else None

        if next_url:
            parsed = urllib.parse.urlparse(next_url)
            query = urllib.parse.parse_qs(parsed.query)
            page_info = query.get("page_info", [None])[0]
            next_url = f"https://{shop}/admin/api/2025-10/orders.json?limit=250&page_info={page_info}" if page_info else None
        
        if not next_url:
            logger.info("Shopify fetch complete: %d total orders, %d pages for %s", len(orders), page_count, shop)

    return orders

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
        doc = {
            "order_id": order["id"],
            "user_id": ObjectId(user_id),
            "company_id": ObjectId(company_id),
            "order_number": order.get("order_number"),
            "name": order.get("name"),
            "shop": shop,
            "created_at": _to_datetime(order.get("created_at")),
            "customer": {
                "id": order.get("customer", {}).get("id"),
                "email": order.get("customer", {}).get("email"),
                "name": f"{order.get('customer', {}).get('first_name', '')} {order.get('customer', {}).get('last_name', '')}".strip(),
                "phone": order.get("customer", {}).get("phone"),
                "default_address": {
                    "address1": order.get("customer", {}).get("default_address", {}).get("address1"),
                    "address2": order.get("customer", {}).get("default_address", {}).get("address2"),
                    "city": order.get("customer", {}).get("default_address", {}).get("city"),
                    "province": order.get("customer", {}).get("default_address", {}).get("province"),
                    "country": order.get("customer", {}).get("default_address", {}).get("country"),
                    "zip": order.get("customer", {}).get("default_address", {}).get("zip"),
                }
            },
            "shipping_address": order.get("shipping_address", {}),
            "billing_address": order.get("billing_address", {}),
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
                {"order_id": doc["order_id"], "shop": shop},
                {"$set": doc},
                upsert=True
            )
        )
    if bulk_ops:
        await db.orders.bulk_write(bulk_ops)
