import requests
from datetime import datetime
from pymongo import UpdateOne
from bson import ObjectId

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

async def fetch_orders_from_shop(shop, access_token):
    """Fetch the 30 most recent orders from a Shopify store using the access token."""
    url = f"https://{shop}/admin/api/2025-10/orders.json?status=any&limit=10&order=created_at desc"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data.get("orders", [])

async def upsert_orders(db, shop, orders):
    """Insert or update orders in the database for a specific shop."""

    user_id, company_id = None, None

    cred = await db.shopify_cred.find_one({"shop": shop})
    if not cred:
        print(f"[!] Shopify credentials not found for shop: {shop}")
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
            "created_at": order.get("created_at"),
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
            "total_price": order.get("total_price"),
            "payment_status": order.get("financial_status"),
            "fulfillment_status": order.get("fulfillment_status"),
            "line_items": [
                {
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
        db.orders.bulk_write(bulk_ops)