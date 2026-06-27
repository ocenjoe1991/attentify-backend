from fastapi import APIRouter, Request, HTTPException, Header, status, BackgroundTasks, Depends, Query, Body
from fastapi.responses import RedirectResponse, JSONResponse
from urllib.parse import urlencode
import hmac, hashlib, requests, base64
import logging
import os
import re
from typing import List, Dict
from datetime import datetime, timezone
import json
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.services.shopify_service import (
    get_all_shopify_creds,
    fetch_access_scopes,
    fetch_orders_from_shop,
    upsert_orders,
    _to_datetime,
)

from math import ceil
from app.db.mongodb import get_database, get_db
from app.core.security import get_current_user
from app.main import sio
from app.core.permissions import (
    PERMISSION_CANCELLATION_WITHOUT_OWNER_APPROVAL,
    PERMISSION_REFUND_WITHOUT_OWNER_APPROVAL,
    has_owner_approval_bypass,
)
from app.core.audit import record_audit_log
from app.utils.datetime_utils import to_utc_iso
import httpx

logger = logging.getLogger("attentify.shopify")

router = APIRouter()

SHOPIFY_API_KEY = os.getenv("SHOPIFY_API_KEY")
SHOPIFY_API_SECRET = os.getenv("SHOPIFY_API_SECRET")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_REDIRECT_URI = os.getenv("SHOPIFY_REDIRECT_URI", "http://localhost:8000/api/v1/shopify/callback")
SHOPIFY_SCOPES = os.getenv(
    "SHOPIFY_SCOPES",
    "read_products,write_products,read_orders,read_all_orders,write_orders,read_customers,write_customers,"
    "read_returns,write_returns,read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders",
)
SHOPIFY_INSTALL_URL=os.getenv("SHOPIFY_INSTALL_URL")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

OWNER_ROLES = {"company_owner", "store_owner"}


def normalized_shopify_scopes() -> str:
    base_scopes = SHOPIFY_SCOPES or ""
    scopes = [scope.strip() for scope in base_scopes.split(",") if scope.strip()]
    for required_scope in ("read_orders", "read_all_orders"):
        if required_scope not in scopes:
            scopes.append(required_scope)
    return ",".join(scopes)


def to_float_amount(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def build_order_action(
    *,
    action_type: str,
    amount,
    current_user: dict,
    membership: dict,
    note: str = "",
    details: dict | None = None,
) -> dict:
    return {
        "type": action_type,
        "amount": to_float_amount(amount),
        "actor_id": str(current_user["_id"]),
        "actor_name": f"{current_user.get('first_name', '')} {current_user.get('last_name', '')}".strip()
        or current_user.get("email", "Unknown user"),
        "actor_role": membership.get("role", "unknown") if membership else "unknown",
        "note": note,
        "details": details or {},
        "created_at": datetime.now(timezone.utc),
    }


def serialize_order_actions(order: dict) -> list[dict]:
    actions = []
    for action in order.get("order_actions", []):
        serialized = dict(action)
        if serialized.get("created_at") and hasattr(serialized["created_at"], "isoformat"):
            serialized["created_at"] = to_utc_iso(serialized["created_at"])
        if serialized.get("actor_id"):
            serialized["actor_id"] = str(serialized["actor_id"])
        actions.append(enrich_action_details_with_line_items(serialized, order))

    for refund in order.get("refunds", []) or []:
        transactions = refund.get("transactions") or []
        amount = round(sum(to_float_amount(transaction.get("amount")) for transaction in transactions), 2)
        if not amount:
            amount = to_float_amount(refund.get("total_refunded"))
        if not amount:
            amount = round(
                sum(
                    to_float_amount(item.get("subtotal")) + to_float_amount(item.get("total_tax"))
                    for item in refund.get("refund_line_items", []) or []
                ),
                2,
            )
        actions.append({
            "type": "refund",
            "amount": amount,
            "actor_name": "Shopify",
            "actor_role": "system",
            "note": refund.get("note") or "Refund recorded in Shopify",
            "details": {
                "source": "shopify",
                "shopify_refund_id": refund.get("id"),
                "order_id": str(order.get("order_id", "")),
                "transactions": transactions,
                "line_items": build_refund_line_items(order, refund),
                "shipping_refund": build_refund_shipping_line(refund),
            },
            "created_at": refund.get("created_at") or refund.get("processed_at") or order.get("updated_at") or "",
        })

    if order.get("cancelled_at"):
        actions.append({
            "type": "cancellation",
            "amount": to_float_amount(order.get("total_price")),
            "actor_name": "Shopify",
            "actor_role": "system",
            "note": order.get("cancel_reason") or "Cancellation recorded in Shopify",
            "details": {
                "source": "shopify",
                "order_id": str(order.get("order_id", "")),
                "cancel_reason": order.get("cancel_reason", ""),
            },
            "created_at": order.get("cancelled_at"),
        })

    if order_is_refunded(order) and not order.get("refunds") and not any(action.get("type") == "refund" for action in actions):
        actions.append({
            "type": "refund",
            "amount": to_float_amount(order.get("total_price")),
            "actor_name": "Shopify",
            "actor_role": "system",
            "note": "Refunded in Shopify; detailed refund record was not available from Shopify.",
            "details": {
                "source": "shopify",
                "inferred": True,
                "order_id": str(order.get("order_id", "")),
            },
            "created_at": order.get("updated_at") or order.get("created_at") or "",
        })
    return sorted(actions, key=lambda item: item.get("created_at", ""), reverse=True)


async def hydrate_shopify_refunds_for_order(db, order: dict) -> dict:
    if order.get("refunds") or not order_is_refunded(order):
        return order

    shop = order.get("shop")
    order_id = order.get("order_id")
    company_id = order.get("company_id")
    if not shop or not order_id or not company_id:
        return order

    cred = await db["shopify_cred"].find_one({"shop": shop, "company_id": company_id})
    access_token = (cred or {}).get("access_token")
    if not access_token:
        return order

    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}/refunds.json"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json",
                },
            )
    except Exception:
        return order

    if response.status_code >= 400:
        return order

    refunds = response.json().get("refunds", [])
    if refunds:
        order["refunds"] = refunds
        await db["orders"].update_one({"_id": order["_id"]}, {"$set": {"refunds": refunds}})
    return order


def stringify_shopify_error(value) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(filter(None, [stringify_shopify_error(item) for item in value]))
    if isinstance(value, dict):
        for key in ("message", "error", "errors"):
            if key in value:
                message = stringify_shopify_error(value.get(key))
                if message:
                    return message
        return " ".join(
            filter(None, [f"{key}: {stringify_shopify_error(item) or item}" for key, item in value.items()])
        )
    return str(value)


def find_order_line_item(order: dict, line_item_id) -> dict:
    line_item_id = str(line_item_id or "")
    for item in order.get("line_items", []) or []:
        if str(item.get("id", "")) == line_item_id:
            return item
    return {}


def refund_shipping_amount(refund: dict) -> float:
    shipping = refund.get("shipping") or {}
    for value in (
        shipping.get("amount"),
        (shipping.get("shop_money") or {}).get("amount"),
        (shipping.get("presentment_money") or {}).get("amount"),
    ):
        amount = to_float_amount(value)
        if amount:
            return amount

    for adjustment in refund.get("order_adjustments", []) or []:
        kind = str(adjustment.get("kind", "")).lower()
        reason = str(adjustment.get("reason", "")).lower()
        if "shipping" not in kind and "shipping" not in reason:
            continue
        amount = to_float_amount(adjustment.get("amount"))
        if not amount:
            amount = to_float_amount((adjustment.get("amount_set") or {}).get("shop_money", {}).get("amount"))
        if amount:
            return abs(amount)
    return 0.0


def format_action_line_item(*, name="", quantity=1, amount=None, line_item_id="", variant_id="") -> dict:
    return {
        "name": name or "Unknown item",
        "quantity": int(quantity or 1),
        "amount": to_float_amount(amount) if amount not in (None, "") else "",
        "line_item_id": str(line_item_id or ""),
        "variant_id": str(variant_id or ""),
    }


def build_refund_line_items(order: dict, refund: dict) -> list[dict]:
    items = []
    for refund_item in refund.get("refund_line_items", []) or []:
        nested_item = refund_item.get("line_item") or {}
        line_item_id = refund_item.get("line_item_id") or nested_item.get("id")
        order_item = find_order_line_item(order, line_item_id)
        items.append(format_action_line_item(
            name=nested_item.get("name") or order_item.get("name"),
            quantity=refund_item.get("quantity"),
            amount=refund_item.get("subtotal"),
            line_item_id=line_item_id,
            variant_id=nested_item.get("variant_id") or order_item.get("variant_id"),
        ))
    return items


def build_refund_shipping_line(refund: dict) -> dict | None:
    amount = refund_shipping_amount(refund)
    if not amount:
        return None
    return {
        "name": "Shipping refund",
        "amount": amount,
    }


def build_selected_line_items(order: dict, selected_items: list[dict]) -> list[dict]:
    items = []
    for selected in selected_items or []:
        line_item_id = selected.get("line_item_id") or selected.get("id")
        order_item = find_order_line_item(order, line_item_id)
        items.append(format_action_line_item(
            name=order_item.get("name"),
            quantity=selected.get("quantity"),
            amount=selected.get("amount") or order_item.get("price"),
            line_item_id=line_item_id,
            variant_id=order_item.get("variant_id"),
        ))
    return items


def enrich_action_details_with_line_items(action: dict, order: dict) -> dict:
    details = dict(action.get("details") or {})
    if details.get("line_items") or details.get("returned_items"):
        action["details"] = details
        return action

    selected_items = details.get("selected_items") or []
    if selected_items:
        line_items = build_selected_line_items(order, selected_items)
        details["line_items"] = line_items
        if action.get("type") in {"return", "exchange"}:
            details["returned_items"] = line_items

    exchange_items = details.get("exchange_items") or []
    if exchange_items:
        details["exchange_items"] = [
            format_action_line_item(
                name=item.get("name") or item.get("title") or f"Variant {item.get('variant_id')}",
                quantity=item.get("quantity"),
                variant_id=item.get("variant_id"),
            )
            for item in exchange_items
        ]

    action["details"] = details
    return action


def order_is_refunded(order: dict) -> bool:
    return any(
        str(order.get(field, "")).lower() == "refunded"
        for field in ("payment_status", "financial_status")
    )


def order_is_cancelled(order: dict) -> bool:
    if order.get("cancelled_at"):
        return True
    return any(
        str(order.get(field, "")).lower() in {"cancelled", "canceled"}
        for field in ("status", "fulfillment_status")
    )


def order_has_cancellation_action(order: dict) -> bool:
    return any(action.get("type") == "cancellation" for action in order.get("order_actions", []))


async def get_order_action_context(db, payload: dict, current_user: dict):
    order_id = payload.get("order_id")
    shop = payload.get("shop")
    if not order_id or not shop:
        return JSONResponse(status_code=400, content={"error": "Missing order_id or shop"})

    shopify_cred = await db["shopify_cred"].find_one({"shop": shop})
    if not shopify_cred or not shopify_cred.get("access_token"):
        return JSONResponse(status_code=404, content={"error": "Shop credentials not found"})

    order_doc = await db["orders"].find_one({"order_id": int(order_id)})
    if not order_doc:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    if order_doc.get("company_id") != shopify_cred.get("company_id"):
        return JSONResponse(status_code=403, content={"error": "Order does not belong to this shop connection"})

    membership = await require_company_member(db, current_user, order_doc["company_id"])
    return order_doc, shopify_cred, membership


async def record_order_action_and_audit(
    db,
    *,
    order_doc: dict,
    current_user: dict,
    membership: dict,
    action_type: str,
    audit_action: str,
    amount,
    note: str,
    message_id: str = "",
    details: dict | None = None,
    order_updates: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    action_details = {
        "order_id": str(order_doc.get("order_id", "")),
        "shop": order_doc.get("shop", ""),
        **(details or {}),
    }
    order_action = build_order_action(
        action_type=action_type,
        amount=amount,
        current_user=current_user,
        membership=membership,
        note=note,
        details=action_details,
    )
    order_action["created_at"] = now

    set_payload = {"updated_at": now, **(order_updates or {})}
    await db["orders"].update_one(
        {"_id": order_doc["_id"]},
        {"$set": set_payload, "$push": {"order_actions": order_action}},
    )

    message = None
    if ObjectId.is_valid(message_id or ""):
        message = await db["messages"].find_one({"_id": ObjectId(message_id), "company_id": order_doc["company_id"]})
        if message:
            await db["messages"].update_one(
                {"_id": message["_id"]},
                {
                    "$push": {
                        "activity": {
                            "type": action_type,
                            "actor_id": current_user["_id"],
                            "note": note,
                            "amount": to_float_amount(amount),
                            "created_at": now,
                        }
                    },
                    "$set": {"last_updated": now},
                },
            )

    await record_audit_log(
        db,
        company_id=order_doc["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown"),
        action=audit_action,
        entity_type="order",
        entity_id=order_doc["_id"],
        ticket=(message or {}).get("ticket", ""),
        customer=order_doc.get("customer", {}).get("email", "") or (message or {}).get("client", ""),
        details={**action_details, "amount": to_float_amount(amount), "message_id": message_id},
    )
    return order_action


async def get_fulfillment_orders(shop: str, access_token: str, order_id: str) -> list[dict]:
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}/fulfillment_orders.json"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json())
    return response.json().get("fulfillment_orders", [])


async def shopify_graphql(shop: str, access_token: str, query: str, variables: dict | None = None) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json())
    data = response.json()
    if data.get("errors"):
        raise HTTPException(status_code=400, detail=data["errors"])
    return data.get("data", {})


def order_gid(order_id) -> str:
    return f"gid://shopify/Order/{int(order_id)}"


def variant_gid(value) -> str:
    value = str(value or "")
    return value if value.startswith("gid://") else f"gid://shopify/ProductVariant/{int(value)}"


async def build_return_line_items(
    shop: str,
    access_token: str,
    order_id,
    selected_items: list[dict],
    note: str,
) -> list[dict]:
    query = """
    query ReturnableFulfillments($id: ID!) {
      order(id: $id) {
        returnableFulfillments(first: 20) {
          edges {
            node {
              returnableFulfillmentLineItems(first: 100) {
                edges {
                  node {
                    quantity
                    fulfillmentLineItem {
                      id
                      lineItem {
                        legacyResourceId
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    data = await shopify_graphql(shop, access_token, query, {"id": order_gid(order_id)})
    selected_by_line_item_id = {
        str(item.get("line_item_id")): int(item.get("quantity") or 1)
        for item in selected_items
        if item.get("line_item_id")
    }
    return_line_items = []
    for edge in data.get("order", {}).get("returnableFulfillments", {}).get("edges", []):
        line_edges = edge.get("node", {}).get("returnableFulfillmentLineItems", {}).get("edges", [])
        for line_edge in line_edges:
            node = line_edge.get("node", {})
            fulfillment_line_item = node.get("fulfillmentLineItem", {})
            line_item_id = str(fulfillment_line_item.get("lineItem", {}).get("legacyResourceId", ""))
            if selected_by_line_item_id and line_item_id not in selected_by_line_item_id:
                continue
            quantity = selected_by_line_item_id.get(line_item_id, node.get("quantity", 1))
            return_line_items.append({
                "fulfillmentLineItemId": fulfillment_line_item.get("id"),
                "quantity": min(int(quantity or 1), int(node.get("quantity") or 1)),
                "returnReasonNote": note[:255],
            })
    if not return_line_items:
        raise HTTPException(status_code=400, detail="No returnable fulfillment line items found for this order")
    return return_line_items


async def create_shopify_return(
    shop: str,
    access_token: str,
    order_id,
    return_line_items: list[dict],
    exchange_line_items: list[dict] | None = None,
) -> dict:
    mutation = """
    mutation ReturnCreate($returnInput: ReturnInput!) {
      returnCreate(returnInput: $returnInput) {
        userErrors {
          field
          message
        }
        return {
          id
          status
          returnLineItems(first: 20) {
            edges {
              node {
                id
                quantity
              }
            }
          }
          exchangeLineItems(first: 20) {
            edges {
              node {
                id
                quantity
                variantId
              }
            }
          }
        }
      }
    }
    """
    return_input = {
        "orderId": order_gid(order_id),
        "returnLineItems": return_line_items,
    }
    if exchange_line_items:
        return_input["exchangeLineItems"] = exchange_line_items
    data = await shopify_graphql(shop, access_token, mutation, {"returnInput": return_input})
    payload = data.get("returnCreate", {})
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise HTTPException(status_code=400, detail=user_errors)
    return payload.get("return", {})


async def send_order_invoice(shop: str, access_token: str, order_id, email: str | None = None) -> dict:
    mutation = """
    mutation OrderInvoiceSend($id: ID!, $email: EmailInput) {
      orderInvoiceSend(id: $id, email: $email) {
        order {
          id
          name
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {"id": order_gid(order_id), "email": {"to": email} if email else None}
    data = await shopify_graphql(shop, access_token, mutation, variables)
    payload = data.get("orderInvoiceSend", {})
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise HTTPException(status_code=400, detail=user_errors)
    return payload.get("order", {})


async def require_company_member(db, current_user: dict, company_id: ObjectId) -> dict:
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": company_id,
        "status": "active",
    })
    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")
    return membership


async def get_governance_settings(db) -> dict:
    settings = await db["admin_settings"].find_one({"key": "admin_governance"}) or {}
    return settings.get("approvals", {})


async def create_approval_request(
    db,
    action_type: str,
    payload: dict,
    current_user: dict,
    company_id: ObjectId,
    message_id: str = "",
) -> dict:
    now = datetime.now(timezone.utc)
    request_doc = {
        "type": action_type,
        "payload": payload,
        "company_id": company_id,
        "message_id": ObjectId(message_id) if ObjectId.is_valid(message_id or "") else None,
        "requested_by": current_user["_id"],
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    result = await db["approval_requests"].insert_one(request_doc)
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": company_id,
        "status": "active",
    })
    message = None
    if request_doc["message_id"]:
        message = await db["messages"].find_one({"_id": request_doc["message_id"], "company_id": company_id})
        await db["messages"].update_one(
            {"_id": request_doc["message_id"], "company_id": company_id},
            {"$set": {"status": "Awaiting Approval", "last_updated": now}},
        )
    await record_audit_log(
        db,
        company_id=company_id,
        actor=current_user,
        actor_role=membership.get("role", "unknown") if membership else "unknown",
        action=f"Requested {action_type} approval",
        entity_type="approval_request",
        entity_id=result.inserted_id,
        ticket=(message or {}).get("ticket", ""),
        customer=(message or {}).get("client", ""),
        details={
            "order_id": payload.get("order_id"),
            "shop": payload.get("shop"),
            "message_id": str(request_doc["message_id"] or ""),
        },
    )
    return {
        "msg": f"{action_type.title()} request is awaiting owner approval.",
        "approval_required": True,
        "approval_request_id": str(result.inserted_id),
    }


def serialize_approval_request(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    doc["company_id"] = str(doc["company_id"])
    doc["created_at"] = to_utc_iso(doc.get("created_at"))
    doc["updated_at"] = to_utc_iso(doc.get("updated_at"))
    doc["processed_at"] = to_utc_iso(doc.get("processed_at"))
    if doc.get("message_id"):
        doc["message_id"] = str(doc["message_id"])
    if doc.get("requested_by"):
        doc["requested_by"] = str(doc["requested_by"])
    if doc.get("processed_by"):
        doc["processed_by"] = str(doc["processed_by"])
    return doc


async def require_company_owner(db, current_user: dict, company_id: ObjectId) -> dict:
    membership = await require_company_member(db, current_user, company_id)
    if membership.get("role") != "company_owner":
        raise HTTPException(status_code=403, detail="Only company owners can process approval requests")
    return membership

class ShopifyAuthHelper:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def build_authorization_url(self, shop: str, redirect_uri: str):
        params = {
            "client_id": self.client_id,
            "scope": normalized_shopify_scopes(),
            "redirect_uri": redirect_uri,
            "state": "secure_random_state",  
        }
        return f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"

    async def exchange_code_for_access_token(self, shop: str, code: str):
        import httpx
        url = f"https://{shop}/admin/oauth/access_token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, data=data)
            r.raise_for_status()
            return r.json()["access_token"]
        
shopify_auth_helper = ShopifyAuthHelper(SHOPIFY_API_KEY, SHOPIFY_API_SECRET)       

#/api/v1/shopify/auth
@router.get("/auth")
async def shopify_auth(request: Request, 
                 user_id: str = Query(...),
                 company_id: str = Query(...),
                 db: AsyncIOMotorDatabase = Depends(get_database),
                ):
    """
    Redirect user to Shopify OAuth consent page.
    Sets session + persistent cookie with user_id/company_id for callback recovery.
    """
    import uuid as _uuid
    
    request.session["user_id"] = user_id
    request.session["company_id"] = company_id

    # Build a proper Shopify OAuth URL with state parameter as fallback
    import base64 as b64
    import json
    state_data = b64.urlsafe_b64encode(
        json.dumps({"user_id": user_id, "company_id": company_id}).encode()
    ).decode()
    shop = request.query_params.get("shop", "")
    if shop:
        redirect_uri = f"{BACKEND_URL}/api/v1/shopify/callback"
        scopes = normalized_shopify_scopes()
        auth_params = {
            "client_id": SHOPIFY_API_KEY,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state_data,
        }
        auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(auth_params)}"
        logger.info("Starting Shopify OAuth for %s with scopes: %s", shop, scopes)
        return RedirectResponse(url=auth_url)
    
    # No shop → use SHOPIFY_INSTALL_URL + store pending record for callback recovery
    await db["shopify_pending"].insert_one({
        "user_id": user_id,
        "company_id": company_id,
        "created_at": datetime.now(timezone.utc)
    })
    logger.info("Starting Shopify install URL flow for company %s", company_id)
    return RedirectResponse(url=SHOPIFY_INSTALL_URL)

@router.get("/install")
def shopify_install(
    request: Request, 
):
    params = dict(request.query_params)
    shop = params.get("shop")
    hmac = params.get("hmac")
    if not shop or not hmac:
        raise HTTPException(status_code=400, detail="Missing 'shop' or 'hmac' parameter")
    
    redirect_uri = f"{BACKEND_URL}/api/v1/shopify/callback"
    auth_url = shopify_auth_helper.build_authorization_url(shop, redirect_uri)
    return RedirectResponse(url=auth_url)

#/api/v1/shopify/callback
@router.get("/callback")
async def shopify_callback(request: Request):
    db = request.app.state.db
    params = dict(request.query_params)
    shop = params.get("shop")
    code = params.get("code")
    hmac_received = params.get("hmac")

    user_id = request.session.get("user_id")
    company_id = request.session.get("company_id")

    # Fallback 1: decode state parameter if session is missing
    if (not user_id or not company_id) and params.get("state"):
        try:
            import base64 as b64
            import json
            state_raw = b64.urlsafe_b64decode(params["state"].encode()).decode()
            state_data = json.loads(state_raw)
            user_id = user_id or state_data.get("user_id")
            company_id = company_id or state_data.get("company_id")
        except Exception:
            pass

    # Fallback 2: find most recent pending record (SHOPIFY_INSTALL_URL route, no cookie)
    if (not user_id or not company_id):
        from datetime import timedelta
        pending = await db["shopify_pending"].find_one(
            {"created_at": {"$gte": datetime.now(timezone.utc) - timedelta(minutes=10)}},
            sort=[("created_at", -1)]
        )
        if pending:
            user_id = user_id or pending.get("user_id")
            company_id = company_id or pending.get("company_id")
            await db["shopify_pending"].delete_many({
                "user_id": pending["user_id"],
                "company_id": pending["company_id"]
            })

    if not shop or not code or not hmac_received or not user_id:
        raise HTTPException(status_code=400, detail="Missing parameters")

    # HMAC verification
    sorted_params = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac"
    )
    generated_hmac = hmac.new(
        SHOPIFY_API_SECRET.encode("utf-8"),
        sorted_params.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(generated_hmac, hmac_received):
        raise HTTPException(status_code=400, detail="Invalid HMAC")

    # Exchange code for access token
    token_url = f"https://{shop}/admin/oauth/access_token"
    data = {
        "client_id": SHOPIFY_API_KEY,
        "client_secret": SHOPIFY_API_SECRET,
        "code": code
    }

    response = requests.post(token_url, json=data)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Token exchange failed")

    access_token = response.json().get("access_token")
    token_scopes = fetch_access_scopes(shop, access_token) if access_token else []
    if "read_all_orders" not in token_scopes:
        logger.warning(
            "New Shopify token for %s does not include read_all_orders after OAuth callback.",
            shop,
        )

    # Register webhooks (orders/create + orders/updated)
    webhook_ids = register_shopify_webhook(shop, access_token)

    await db.shopify_cred.update_one(
        {"shop": shop, "company_id": ObjectId(company_id)},
        {
            "$set": {
                "shop": shop,
                "access_token": access_token,
                "status": "connected",
                "user_id": ObjectId(user_id),
                "company_id": ObjectId(company_id),
                "webhook_id": webhook_ids.get("create_id") if webhook_ids else None,
                "webhook_update_id": webhook_ids.get("update_id") if webhook_ids else None,
                "last_checked_scopes": token_scopes,
                "has_read_all_orders": "read_all_orders" in token_scopes,
            },
            "$unset": {"last_synced_at": ""},
        },
        upsert=True
    )
    actor = await db["users"].find_one({"_id": ObjectId(user_id)})
    membership = await db["memberships"].find_one({
        "user_id": ObjectId(user_id),
        "company_id": ObjectId(company_id),
        "status": "active",
    })
    cred = await db.shopify_cred.find_one({"shop": shop, "company_id": ObjectId(company_id)})
    await record_audit_log(
        db,
        company_id=ObjectId(company_id),
        actor=actor,
        actor_role=membership.get("role", "unknown") if membership else "unknown",
        action="Connected Shopify store",
        entity_type="shopify_cred",
        entity_id=cred["_id"] if cred else None,
        details={"shop": shop},
    )

    redirect_frontend_url = f"{FRONTEND_URL}/shopify/success?shop={shop}"
    return RedirectResponse(url=redirect_frontend_url)

def decode_host_func(base64_host: str) -> str:
    try:
        padded = base64_host + "=" * ((4 - len(base64_host) % 4) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        return decoded_bytes.decode("utf-8")
    except Exception as ex:
        logger.error("Error decoding Base64 host: %s", ex)
        return ""
    
#/api/v1/shopify/orders
@router.get("/orders1")
def get_shopify_orders(request: Request):
    shop = request.query_params.get("shop")
    if not shop:
        raise HTTPException(status_code=400, detail="Missing 'shop' parameter")

    db = request.app.state.db
    shopify_cred = db.shopify_cred.find_one({"shop": shop})
    if not shopify_cred or "access_token" not in shopify_cred:
        raise HTTPException(status_code=401, detail="Shop not authenticated")

    access_token = shopify_cred["access_token"]
    orders_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json"

    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }

    response = requests.get(orders_url, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Failed to fetch orders")

    return response.json()

@router.get("/", response_model=List[dict])
async def list_shopify_cred(request: Request, current_user: dict = Depends(get_current_user)) :
    db = request.app.state.db
    cursor = db.shopify_cred.find({"user_id": current_user["_id"]})
    docs = []
    async for doc in cursor:
        doc['_id'] = str(doc['_id'])
        doc['user_id'] = str(doc['user_id'])  # Convert ObjectId to string
        doc['company_id'] = str(doc['company_id'])  # Convert ObjectId to string
        doc["created_at"] = to_utc_iso(doc.get("created_at"))
        doc["updated_at"] = to_utc_iso(doc.get("updated_at"))
        docs.append(doc)
    return docs

@router.get("/company", response_model=List[dict])
async def list_company_shopify_cred(
    request: Request,
    current_user: dict = Depends(get_current_user),
    company_id: str = None  # optional query parameter
):
    db = request.app.state.db

    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company ID")

    await require_company_member(db, current_user, ObjectId(company_id))

    cursor = db.shopify_cred.find({
        "company_id": ObjectId(company_id),
        "status": {"$ne": "disconnected"},
    })
    docs = []

    async for doc in cursor:
        doc['_id'] = str(doc['_id'])
        doc['user_id'] = str(doc['user_id'])
        doc['company_id'] = str(doc['company_id'])
        doc["created_at"] = to_utc_iso(doc.get("created_at"))
        doc["updated_at"] = to_utc_iso(doc.get("updated_at"))
        docs.append(doc)

    return docs

@router.delete("/{shopify_id}")
async def disconnect_shopify_cred(
    shopify_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(shopify_id):
        raise HTTPException(status_code=400, detail="Invalid Shopify credential ID")
    db = request.app.state.db

    # Find the credential by ID
    cred = await db.shopify_cred.find_one({"_id": ObjectId(shopify_id)})
    if not cred:
        raise HTTPException(status_code=404, detail="Shopify credential not found")
    membership = await require_company_member(db, current_user, cred["company_id"])
    if membership.get("role") not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Only owners can remove Shopify stores")

    webhook_id = cred.get("webhook_id")
    shop = cred.get("shop")
    access_token = cred.get("access_token")

    # If webhook_id exists, attempt to delete the webhook from Shopify
    if webhook_id and shop and access_token:
        webhook_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/webhooks/{webhook_id}.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }

        try:
            response = requests.delete(webhook_url, headers=headers)
            if response.status_code == 200:
                logger.info("Webhook %s deleted successfully for %s", webhook_id, shop)
            elif response.status_code == 404:
                logger.warning("Webhook %s not found in Shopify for %s (may already be deleted)", webhook_id, shop)
            else:
                logger.error("Webhook delete failed for %s: %s %s", shop, response.status_code, response.text)
        except requests.RequestException as e:
            logger.error("Webhook delete exception for %s: %s", shop, e)

    # Keep the store row so users can clearly see and reconnect known stores.
    result = await db.shopify_cred.update_one(
        {"_id": ObjectId(shopify_id)},
        {
            "$set": {
                "status": "disconnected",
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {
                "access_token": "",
                "webhook_id": "",
                "webhook_update_id": "",
                "last_synced_at": "",
                "last_checked_scopes": "",
                "has_read_all_orders": "",
            },
        },
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Shopify credential not found")

    await record_audit_log(
        db,
        company_id=cred["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown"),
        action="Disconnected Shopify store",
        entity_type="shopify_cred",
        entity_id=ObjectId(shopify_id),
        details={"shop": shop},
    )

    return {"detail": "Disconnected successfully"}

# Register Shopify Webhook
def _register_single_webhook(shop: str, access_token: str, topic: str, address: str) -> str | None:
    """Register a single webhook topic. Returns webhook ID or None."""
    webhook_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/webhooks.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    data = {
        "webhook": {
            "topic": topic,
            "address": address,
            "format": "json"
        }
    }
    try:
        response = requests.post(webhook_url, json=data, headers=headers)
    except requests.RequestException as e:
        logger.error("Webhook request exception for %s (%s): %s", shop, topic, e)
        return None
    if response.status_code == 201:
        webhook_id = response.json().get("webhook", {}).get("id")
        logger.info("Webhook '%s' registered for %s (ID: %s)", topic, shop, webhook_id)
        return webhook_id
    else:
        logger.error("Webhook registration failed for %s (%s): %s %s", shop, topic, response.status_code, response.text)
        return None


def register_shopify_webhook(shop: str, access_token: str) -> dict | None:
    """Register both orders/create and orders/updated webhooks.
    Returns dict with 'create_id' and 'update_id' keys, or None if both fail."""
    create_id = _register_single_webhook(
        shop, access_token,
        "orders/create",
        f"{BACKEND_URL}/api/v1/shopify/webhook/orders_create"
    )
    update_id = _register_single_webhook(
        shop, access_token,
        "orders/updated",
        f"{BACKEND_URL}/api/v1/shopify/webhook/orders_updated"
    )
    if create_id or update_id:
        return {"create_id": create_id, "update_id": update_id}
    return None

# Delete Shopify Webhook
def delete_shopify_webhook(shop: str, access_token: str, webhook_id: str):
    webhook_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/webhooks/{webhook_id}.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }

    try:
        response = requests.delete(webhook_url, headers=headers)
    except requests.RequestException as e:
        logger.error("Webhook delete exception for %s: %s", shop, e)
        return False

    if response.status_code == 200:
        logger.info("Webhook %s deleted successfully for %s", webhook_id, shop)
        return True
    else:
        logger.error("Webhook delete failed for %s: %s %s", shop, response.status_code, response.text)
        return False
    
@router.post("/webhook/orders_create")
async def shopify_orders_create_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...)
):
    try:
        raw_body = await request.body()
        
        # --- HMAC Verification ---
        computed_hmac = base64.b64encode(
            hmac.new(
                SHOPIFY_API_SECRET.encode("utf-8"),
                raw_body,
                hashlib.sha256
            ).digest()
        ).decode()
        # Shopify sends the HMAC header as base64 (case-insensitive)
        if not hmac.compare_digest(computed_hmac, x_shopify_hmac_sha256):
            logger.warning("Invalid HMAC received from shop: %s", x_shopify_shop_domain)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC")
        
        data = json.loads(raw_body)

        db = request.app.state.db
        user_id, company_id = None, None

        cred = await db.shopify_cred.find_one({"shop": x_shopify_shop_domain})
        if not cred:
            logger.warning("Shopify credentials not found for shop: %s", x_shopify_shop_domain)
            user_id = None
            company_id = None
        else:
            user_id = cred.get("user_id")
            company_id = cred.get("company_id")

        order_document = {
            "shop": x_shopify_shop_domain,
            "order_id": data["id"],
            "user_id": ObjectId(user_id),
            "company_id": ObjectId(company_id),
            "order_number": data.get("order_number"),
            "name": data.get("name"),
            "created_at": _to_datetime(data.get("created_at")),
            "customer": {
                "id": data.get("customer", {}).get("id"),
                "email": data.get("customer", {}).get("email"),
                "name": f"{data.get('customer', {}).get('first_name', '')} {data.get('customer', {}).get('last_name', '')}".strip(),
                "phone": data.get("customer", {}).get("phone"),
                "default_address": {
                    "address1": data.get("customer", {}).get("default_address", {}).get("address1"),
                    "address2": data.get("customer", {}).get("default_address", {}).get("address2"),
                    "city": data.get("customer", {}).get("default_address", {}).get("city"),
                    "province": data.get("customer", {}).get("default_address", {}).get("province"),
                    "country": data.get("customer", {}).get("default_address", {}).get("country"),
                    "zip": data.get("customer", {}).get("default_address", {}).get("zip"),
                }
            },
            "shipping_address": data.get("shipping_address", {}),
            "billing_address": data.get("billing_address", {}),
            "line_items": [
                {   
                    "id": item.get("id"),
                    "product_id": item.get("product_id"),
                    "name": item.get("name"),
                    "quantity": item.get("quantity"),
                    "price": item.get("price")
                } for item in data.get("line_items", [])
            ],
            "total_price": data.get("total_price"),
            "total_shipping_price": (
                    data.get("total_shipping_price_set", {})
                        .get("shop_money", {})
                        .get("amount", 0)
                    ),
            "payment_status": data.get("financial_status"),
            "fulfillment_status": data.get("fulfillment_status"),
            "cancelled_at": data.get("cancelled_at"),
            "cancel_reason": data.get("cancel_reason"),
            "closed_at": data.get("closed_at"),
            "refunds": data.get("refunds", []),
            "fulfillments": data.get("fulfillments", []),
            "updated_at": data.get("updated_at")
        }

        
        # async Motor: must await db operations
        if not await db.orders.find_one({"order_id": order_document["order_id"]}):
            logger.info("Order %s not found in %s, inserting new document", order_document['order_id'], order_document['shop'])
            await db.orders.insert_one(order_document)

        return {"success": True}
    except Exception as e:
        logger.error("Error processing webhook: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Webhook processing failed: {str(e)}")


@router.post("/webhook/orders_updated")
async def shopify_orders_updated_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...)
):
    """Handle orders/updated webhook from Shopify – upsert the changed order."""
    try:
        raw_body = await request.body()

        # HMAC verification
        computed_hmac = base64.b64encode(
            hmac.new(
                SHOPIFY_API_SECRET.encode("utf-8"),
                raw_body,
                hashlib.sha256
            ).digest()
        ).decode()
        if not hmac.compare_digest(computed_hmac, x_shopify_hmac_sha256):
            logger.warning("Invalid HMAC received from shop: %s", x_shopify_shop_domain)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC")

        data = json.loads(raw_body)
        db = request.app.state.db

        cred = await db.shopify_cred.find_one({"shop": x_shopify_shop_domain})
        user_id = cred.get("user_id") if cred else None
        company_id = cred.get("company_id") if cred else None

        # Upsert the single updated order using the same upsert_orders helper
        from app.services.shopify_service import upsert_orders
        await upsert_orders(db, x_shopify_shop_domain, [data])
        db_order = await db["orders"].find_one({
            "shop": x_shopify_shop_domain,
            "order_id": data.get("id"),
        })
        if db_order:
            from app.api.v1.message import build_order_snapshot
            order_snapshot = await build_order_snapshot(db, db_order)
            now = datetime.now(timezone.utc)
            await db["messages"].update_many(
                {
                    "company_id": db_order.get("company_id"),
                    "order_info.confirmed": True,
                    "$or": [
                        {"order_info.order_id": db_order.get("name")},
                        {"matched_order_name": db_order.get("name")},
                        {"matched_order_id": str(db_order.get("order_id", ""))},
                    ],
                },
                {"$set": {
                    "order_info.shopify_order": order_snapshot,
                    "order_info.order_snapshot_updated_at": order_snapshot.get("updated_at"),
                    "order_info.analyzed_at": now,
                    "order_info.analysis_source": "shopify_webhook",
                    "order_analysis.status": "cached",
                    "order_analysis.source": "shopify_webhook",
                    "order_analysis.updated_at": now,
                }},
            )

        logger.info("Order %s updated via webhook for %s", data.get("id"), x_shopify_shop_domain)
        return {"success": True}
    except Exception as e:
        logger.error("Error processing orders/updated webhook: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Webhook processing failed: {str(e)}")
    
# Endpoint: Get all orders (for all stores)
@router.get("/orders")
async def get_orders(
    request: Request,
    search: str = Query("", description="Search by order name or customer email"),
    shop: str = Query("", description="Filter by shop"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Page size"),
    sort_by: str = Query("date", description="order, date, payment_status, fulfillment_status"),
    sort_order: str = Query("desc", description="asc or desc"),
    company_id: str = Query("", description="Company ID"),
    email: str = Query("", description="Email"),
    include_actions: bool = Query(False, description="Include detailed order action history"),
    current_user: dict = Depends(get_current_user),
):
    db = request.app.state.db

    # Build filter query
    filter_query = {}
    if search.strip():
        search_value = search.strip()
        search_regex = {"$regex": re.escape(search_value), "$options": "i"}
        search_or = [
            {"name": search_regex},
            {"customer.email": search_regex},
            {"customer.name": search_regex},
            {"email": search_regex},
            {"contact_email": search_regex},
            {"customerEmail": search_regex},
            {"shop": search_regex},
        ]
        numeric_search = search_value.lstrip("#")
        if numeric_search.isdigit():
            numeric_value = int(numeric_search)
            search_or.extend([
                {"order_id": numeric_value},
                {"order_number": numeric_value},
            ])
        filter_query["$or"] = [
            *search_or
        ]
    if shop:
        filter_query["shop"] = shop
    if company_id:
        if not ObjectId.is_valid(company_id):
            raise HTTPException(status_code=400, detail="Invalid company ID")
        await require_company_member(db, current_user, ObjectId(company_id))
        filter_query["company_id"] = ObjectId(company_id)
    else:
        memberships = await db["memberships"].find({
            "user_id": current_user["_id"],
            "status": "active",
        }).to_list(length=100)
        filter_query["company_id"] = {"$in": [m["company_id"] for m in memberships]}
    if email:
        filter_query["customer.email"] = email

    # Count total documents for pagination
    total_count = await db.orders.count_documents(filter_query)
    totalPages = ceil(total_count / size)

    sort_fields = {
        "order": "name",
        "date": "created_at",
        "payment_status": "payment_status",
        "fulfillment_status": "fulfillment_status",
    }
    sort_field = sort_fields.get(sort_by, "created_at")
    sort_direction = 1 if sort_order == "asc" else -1

    # Fetch paginated orders sorted by the requested table column
    projection = None
    if not include_actions:
        projection = {
            "line_items": 0,
            "refunds": 0,
            "fulfillments": 0,
            "order_actions": 0,
        }

    cursor = (
        db.orders.find(filter_query, projection)
        .sort([(sort_field, sort_direction), ("_id", sort_direction)])
        .skip((page - 1) * size)
        .limit(size)
    )
    orders = []
    async for doc in cursor:
        if include_actions:
            doc = await hydrate_shopify_refunds_for_order(db, doc)
            doc["order_actions"] = serialize_order_actions(doc)
        else:
            doc.pop("order_actions", None)
            doc.pop("refunds", None)
            doc.pop("fulfillments", None)
            doc.pop("line_items", None)
        doc['_id'] = str(doc['_id'])
        doc['user_id'] = str(doc['user_id'])
        doc['company_id'] = str(doc['company_id'])
        orders.append(doc)

    return {
        "orders": orders,
        "totalPages": totalPages
    }

# Endpoint: Sync orders from all stores
@router.post("/orders/sync")
async def sync_orders(
    background_tasks: BackgroundTasks,
    company_id: str = Body(..., embed=True),
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")
    membership = await require_company_member(db, current_user, ObjectId(company_id))
    if membership.get("role") not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Only owners can sync Shopify orders")
    background_tasks.add_task(sync_company_orders, ObjectId(company_id))
    return {"msg": "Sync started."}

# Background job: fetch and upsert all orders for all stores
async def sync_all_stores_orders():
    """Background incremental sync – only fetches orders updated since last_synced_at."""
    db = get_db()
    creds = await get_all_shopify_creds(db)
    now = datetime.now(timezone.utc)
    for cred in creds:
        shop = cred.get("shop")
        access_token = cred.get("access_token")
        if not shop or not access_token:
            continue
        try:
            scopes = fetch_access_scopes(shop, access_token)
            if "read_all_orders" not in scopes:
                logger.warning(
                    "Shopify token for %s does not include read_all_orders; historical orders may be limited to recent orders.",
                    shop,
                )
            last_synced = cred.get("last_synced_at")
            updated_at_min = last_synced.isoformat() if last_synced else None
            orders = await fetch_orders_from_shop(shop, access_token, updated_at_min)
            await upsert_orders(db, shop, orders)
            await db["shopify_cred"].update_one(
                {"shop": shop},
                {"$set": {
                    "last_synced_at": now,
                    "last_checked_scopes": scopes,
                    "has_read_all_orders": "read_all_orders" in scopes,
                }}
            )
            logger.info("Synced %d orders for shop %s (since %s)", len(orders), shop, updated_at_min or "beginning")
        except Exception as e:
            logger.error("Error syncing %s: %s", shop, e)


async def sync_company_orders(company_id: ObjectId):
    """Incremental sync – only fetches orders updated since last_synced_at."""
    db = get_db()
    now = datetime.now(timezone.utc)
    cursor = db["shopify_cred"].find({"company_id": company_id, "status": "connected"})
    async for cred in cursor:
        shop = cred.get("shop")
        access_token = cred.get("access_token")
        if not shop or not access_token:
            continue
        try:
            scopes = fetch_access_scopes(shop, access_token)
            if "read_all_orders" not in scopes:
                logger.warning(
                    "Shopify token for %s does not include read_all_orders; historical orders may be limited to recent orders.",
                    shop,
                )
            last_synced = cred.get("last_synced_at")
            updated_at_min = last_synced.isoformat() if last_synced else None
            orders = await fetch_orders_from_shop(shop, access_token, updated_at_min)
            await upsert_orders(db, shop, orders)
            await db["shopify_cred"].update_one(
                {"shop": shop},
                {"$set": {
                    "last_synced_at": now,
                    "last_checked_scopes": scopes,
                    "has_read_all_orders": "read_all_orders" in scopes,
                }}
            )
            logger.info("Synced %d orders for shop %s (since %s)", len(orders), shop, updated_at_min or "beginning")
        except Exception as e:
            logger.error("Error syncing %s: %s", shop, e)
    # Notify clients that sync completed for this company
    await sio.emit("shopify_sync_complete", {"company_id": str(company_id)})


@router.get("/approval-requests")
async def list_approval_requests(
    company_id: str = Query(...),
    status_filter: str = Query("pending"),
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")
    await require_company_owner(db, current_user, ObjectId(company_id))

    query = {"company_id": ObjectId(company_id)}
    if status_filter != "all":
        query["status"] = status_filter

    cursor = db["approval_requests"].find(query).sort("created_at", -1).limit(100)
    requests_list = []
    async for doc in cursor:
        requests_list.append(serialize_approval_request(doc))
    return {"requests": requests_list}


@router.post("/approval-requests/{request_id}/reject")
async def reject_approval_request(
    request_id: str,
    payload: dict = Body(default={}),
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(request_id):
        raise HTTPException(status_code=400, detail="Invalid approval request ID")

    request_doc = await db["approval_requests"].find_one({"_id": ObjectId(request_id)})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Approval request not found")
    await require_company_owner(db, current_user, request_doc["company_id"])
    if request_doc.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Approval request has already been processed")

    now = datetime.now(timezone.utc)
    await db["approval_requests"].update_one(
        {"_id": request_doc["_id"]},
        {
            "$set": {
                "status": "rejected",
                "rejection_reason": (payload.get("reason") or "").strip(),
                "processed_by": current_user["_id"],
                "updated_at": now,
            }
        },
    )
    if request_doc.get("message_id"):
        await db["messages"].update_one(
            {"_id": request_doc["message_id"], "company_id": request_doc["company_id"]},
            {"$set": {"status": "Open", "last_updated": now}},
        )
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": request_doc["company_id"],
        "status": "active",
    })
    message = await db["messages"].find_one({"_id": request_doc.get("message_id")}) if request_doc.get("message_id") else None
    await record_audit_log(
        db,
        company_id=request_doc["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown") if membership else "unknown",
        action="Rejected approval request",
        entity_type="approval_request",
        entity_id=request_doc["_id"],
        ticket=(message or {}).get("ticket", ""),
        customer=(message or {}).get("client", ""),
        details={"type": request_doc.get("type"), "reason": (payload.get("reason") or "").strip()},
    )
    return {"msg": "Approval request rejected."}


@router.post("/approval-requests/{request_id}/approve")
async def approve_approval_request(
    request_id: str,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_user),
):
    if not ObjectId.is_valid(request_id):
        raise HTTPException(status_code=400, detail="Invalid approval request ID")

    request_doc = await db["approval_requests"].find_one({"_id": ObjectId(request_id)})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Approval request not found")
    await require_company_owner(db, current_user, request_doc["company_id"])
    if request_doc.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Approval request has already been processed")

    action_type = request_doc.get("type")
    if action_type == "refund":
        result = await refund_order(request_doc.get("payload", {}), current_user)
    elif action_type == "cancellation":
        result = await cancel_order(request_doc.get("payload", {}), current_user)
    else:
        raise HTTPException(status_code=400, detail="Unsupported approval request type")

    if isinstance(result, JSONResponse) and result.status_code >= 400:
        return result

    now = datetime.now(timezone.utc)
    await db["approval_requests"].update_one(
        {"_id": request_doc["_id"]},
        {
            "$set": {
                "status": "approved",
                "processed_by": current_user["_id"],
                "updated_at": now,
            }
        },
    )
    if request_doc.get("message_id"):
        await db["messages"].update_one(
            {"_id": request_doc["message_id"], "company_id": request_doc["company_id"]},
            {"$set": {"status": "In Progress", "last_updated": now}},
        )
    membership = await db["memberships"].find_one({
        "user_id": current_user["_id"],
        "company_id": request_doc["company_id"],
        "status": "active",
    })
    message = await db["messages"].find_one({"_id": request_doc.get("message_id")}) if request_doc.get("message_id") else None
    await record_audit_log(
        db,
        company_id=request_doc["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown") if membership else "unknown",
        action="Approved approval request",
        entity_type="approval_request",
        entity_id=request_doc["_id"],
        ticket=(message or {}).get("ticket", ""),
        customer=(message or {}).get("client", ""),
        details={"type": request_doc.get("type")},
    )
    return result

# /shopify/order/refund
@router.post("/order/refund")
async def refund_order(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()

    order_id = payload.get("order_id")
    shop = payload.get("shop")
    selected_items = payload.get("selected_items", [])
    refund_shipping = payload.get("refund_shipping")  # optional override
    refund_amount = payload.get("refund_amount")
    note = (payload.get("note") or "").strip()
    message_id = payload.get("message_id", "")

    if not order_id or not shop:
        return JSONResponse(status_code=400, content={"error": "Missing order_id or shop"})
    if not selected_items and refund_shipping in (None, "", 0):
        return JSONResponse(status_code=400, content={"error": "Select at least one item or shipping refund"})
    if not note:
        return JSONResponse(status_code=400, content={"error": "Refund note is required"})

    shopify_cred = await db["shopify_cred"].find_one({"shop": shop})
    if not shopify_cred:
        return JSONResponse(status_code=404, content={"error": "Shop credentials not found"})

    access_token = shopify_cred.get("access_token")

    order = await db["orders"].find_one({"order_id": int(order_id)})
    if not order:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    if order.get("company_id") != shopify_cred.get("company_id"):
        return JSONResponse(status_code=403, content={"error": "Order does not belong to this shop connection"})
    if order_is_refunded(order):
        return JSONResponse(status_code=400, content={"error": "This order is already refunded."})
    if order_is_cancelled(order):
        return JSONResponse(status_code=400, content={"error": "This order is already cancelled."})

    membership = await require_company_member(db, current_user, order["company_id"])
    approvals = await get_governance_settings(db)
    amount_for_approval = float(refund_amount or 0)
    can_bypass_refund_approval = has_owner_approval_bypass(
        membership,
        PERMISSION_REFUND_WITHOUT_OWNER_APPROVAL,
    )
    requires_owner = approvals.get("refund_requires_owner", True) and not can_bypass_refund_approval
    high_value_requires_owner = (
        approvals.get("high_value_refund_requires_owner", True)
        and amount_for_approval >= float(approvals.get("high_value_refund_threshold", 100))
        and not can_bypass_refund_approval
    )
    if requires_owner or high_value_requires_owner:
        return JSONResponse(
            status_code=202,
            content=await create_approval_request(
                db,
                "refund",
                payload,
                current_user,
                order["company_id"],
                message_id,
            ),
        )

    # STEP 1: Build refund line items for calculate.json
    refund_line_items = [
        {
            "line_item_id": item["line_item_id"],
            "quantity": item["quantity"]
        }
        for item in selected_items
    ]

    # STEP 2: Prepare calculate payload
    calculate_payload = {
        "refund": {
            "refund_line_items": refund_line_items,
            "shipping": {
                "amount": float(refund_shipping) if refund_shipping else None
            }
        }
    }

    calculate_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/refunds/calculate.json"

    async with httpx.AsyncClient() as client:
        calc_response = await client.post(
            calculate_url,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json=calculate_payload,
        )

    if calc_response.status_code >= 400:
        logger.error("Calculate refund failed: %s %s", calc_response.status_code, calc_response.text)
        return JSONResponse(status_code=calc_response.status_code, content=calc_response.json())

    calc_refund = calc_response.json()["refund"]

    corrected_transactions = []

    for t in calc_refund["transactions"]:
        transaction_amount = t["amount"]
        if refund_amount not in (None, ""):
            transaction_amount = str(min(float(refund_amount), float(t["amount"])))
        corrected_transactions.append({
            "kind": "refund",
            "parent_id": t["parent_id"],
            "amount": transaction_amount,
            "gateway": t["gateway"]
        })
    actual_refund_amount = round(
        sum(to_float_amount(transaction.get("amount")) for transaction in corrected_transactions),
        2,
    )

    # STEP 3: Use Shopify's calculated refund exactly
    final_payload = {
        "refund": {
            "note": note,
            "refund_line_items": calc_refund["refund_line_items"],
            "shipping": calc_refund["shipping"],
            "transactions": corrected_transactions,  # fixed
            "notify": True
        }
    }

    # STEP 4: Submit refund
    refund_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/refunds.json"

    async with httpx.AsyncClient() as client:
        refund_response = await client.post(
            refund_url,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json=final_payload,
        )

    if refund_response.status_code >= 400:
        logger.error("Refund failed: %s %s", refund_response.status_code, refund_response.text)
        return JSONResponse(status_code=refund_response.status_code, content=refund_response.json())

    now = datetime.now(timezone.utc)
    order_action = build_order_action(
        action_type="refund",
        amount=actual_refund_amount,
        current_user=current_user,
        membership=membership,
        note=note,
        details={
            "order_id": order_id,
            "shop": shop,
            "refund_shipping": refund_shipping,
            "selected_items": selected_items,
        },
    )
    await db["orders"].update_one(
        {"_id": order["_id"]},
        {
            "$set": {"updated_at": now, "last_refund_at": now},
            "$push": {"order_actions": order_action},
        },
    )
    if ObjectId.is_valid(message_id or ""):
        await db["messages"].update_one(
            {"_id": ObjectId(message_id), "company_id": order["company_id"]},
            {
                "$push": {
                    "activity": {
                        "type": "refund",
                        "actor_id": current_user["_id"],
                        "note": note,
                        "created_at": now,
                    }
                },
                "$set": {"last_updated": now},
            },
        )
    message = await db["messages"].find_one({"_id": ObjectId(message_id), "company_id": order["company_id"]}) if ObjectId.is_valid(message_id or "") else None
    await record_audit_log(
        db,
        company_id=order["company_id"],
        actor=current_user,
        actor_role=membership.get("role", "unknown"),
        action="Processed refund",
        entity_type="order",
        entity_id=order["_id"],
        ticket=(message or {}).get("ticket", ""),
        customer=order.get("customer", {}).get("email", "") or (message or {}).get("client", ""),
        details={"order_id": order_id, "shop": shop, "amount": actual_refund_amount, "message_id": message_id},
    )

    return {
        "msg": "Refund processed successfully",
        "shopify_response": refund_response.json()
    }

@router.post("/order/cancel")
async def cancel_order(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()

    # --- Step 1: Validate input ---
    order_id = payload.get("order_id")
    shop = payload.get("shop")
    note = (payload.get("note") or "").strip()
    message_id = payload.get("message_id", "")
    
    if not order_id or not shop:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing order_id or shop"},
        )
    if not note:
        return JSONResponse(status_code=400, content={"error": "Cancellation note is required"})

    # --- Step 2: Get Shopify credentials ---
    shopify_cred = await db.shopify_cred.find_one({"shop": shop})
    if not shopify_cred or "access_token" not in shopify_cred:
        return JSONResponse(
            status_code=404,
            content={"error": f"Shop credentials not found for shop: {shop}"},
        )

    access_token = shopify_cred["access_token"]
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

    # --- Step 3: Get order info from DB ---
    order_doc = await db.orders.find_one({"order_id": int(order_id)})

    if not order_doc:
        return JSONResponse(
            status_code=404,
            content={"error": f"Order {order_id} not found in database."},
        )
    if order_doc.get("company_id") != shopify_cred.get("company_id"):
        return JSONResponse(status_code=403, content={"error": "Order does not belong to this shop connection"})
    if order_is_cancelled(order_doc):
        return JSONResponse(
            status_code=400,
            content={"error": f"Order {order_id} is already cancelled."},
        )
    if order_is_refunded(order_doc):
        return JSONResponse(
            status_code=400,
            content={"error": "This order is already refunded."},
        )
    if order_has_cancellation_action(order_doc):
        return JSONResponse(
            status_code=400,
            content={"error": "Cancellation has already been recorded for this order."},
        )

    membership = await require_company_member(db, current_user, order_doc["company_id"])
    approvals = await get_governance_settings(db)
    if approvals.get("cancellation_requires_owner", True) and not has_owner_approval_bypass(
        membership,
        PERMISSION_CANCELLATION_WITHOUT_OWNER_APPROVAL,
    ):
        return JSONResponse(
            status_code=202,
            content=await create_approval_request(
                db,
                "cancellation",
                payload,
                current_user,
                order_doc["company_id"],
                message_id,
            ),
        )

    # --- Step 5: Call Shopify Cancel API ---
    cancel_url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}/cancel.json"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(cancel_url, headers=headers)
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"Failed to reach Shopify API: {str(e)}"},
            )

    # --- Step 6: Handle Shopify response ---
    if response.status_code == 200:
        data = response.json()
        cancelled_at = datetime.now(timezone.utc)
        cancellation_amount = to_float_amount(order_doc.get("total_price"))
        order_action = build_order_action(
            action_type="cancellation",
            amount=cancellation_amount,
            current_user=current_user,
            membership=membership,
            note=note,
            details={"order_id": order_id, "shop": shop},
        )

        # Optionally update local DB to mark as cancelled
        await db.orders.update_one(
            {"order_id": int(order_id)},
            {
                "$set": {"fulfillment_status": "canceled", "cancelled_at": cancelled_at},
                "$push": {"order_actions": order_action},
            },
        )
        if ObjectId.is_valid(message_id or ""):
            await db["messages"].update_one(
                {"_id": ObjectId(message_id), "company_id": order_doc["company_id"]},
                {
                    "$push": {
                        "activity": {
                            "type": "cancellation",
                            "actor_id": current_user["_id"],
                            "note": note,
                            "created_at": cancelled_at,
                        }
                    },
                    "$set": {"last_updated": cancelled_at},
                },
            )
        message = await db["messages"].find_one({"_id": ObjectId(message_id), "company_id": order_doc["company_id"]}) if ObjectId.is_valid(message_id or "") else None
        await record_audit_log(
            db,
            company_id=order_doc["company_id"],
            actor=current_user,
            actor_role=membership.get("role", "unknown"),
            action="Cancelled order",
            entity_type="order",
            entity_id=order_doc["_id"],
            ticket=(message or {}).get("ticket", ""),
            customer=order_doc.get("customer", {}).get("email", "") or (message or {}).get("client", ""),
            details={"order_id": order_id, "shop": shop, "amount": cancellation_amount, "message_id": message_id},
        )

        return {
            "msg": f"Order {order_id} cancelled successfully.",
            "shopify_response": data,
        }

    # --- Step 7: Shopify error ---
    try:
        error_data = response.json()
    except Exception:
        error_data = {"error": response.text}

    return JSONResponse(
        status_code=response.status_code,
        content={
            "error": stringify_shopify_error(error_data) or "Failed to cancel order in Shopify.",
            "details": error_data,
        },
    )


@router.post("/order/return")
async def create_return_action(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    context = await get_order_action_context(db, payload, current_user)
    if isinstance(context, JSONResponse):
        return context
    order_doc, shopify_cred, membership = context

    note = (payload.get("note") or "").strip()
    if not note:
        return JSONResponse(status_code=400, content={"error": "Return note is required"})

    amount = payload.get("amount") or 0
    try:
        return_line_items = await build_return_line_items(
            payload.get("shop"),
            shopify_cred["access_token"],
            payload.get("order_id"),
            payload.get("selected_items", []),
            note,
        )
        shopify_return = await create_shopify_return(
            payload.get("shop"),
            shopify_cred["access_token"],
            payload.get("order_id"),
            return_line_items,
        )
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    await record_order_action_and_audit(
        db,
        order_doc=order_doc,
        current_user=current_user,
        membership=membership,
        action_type="return",
        audit_action="Created return",
        amount=amount,
        note=note,
        message_id=payload.get("message_id", ""),
        details={
            "selected_items": payload.get("selected_items", []),
            "shopify_return_id": shopify_return.get("id"),
        },
    )
    return {"msg": "Return created successfully.", "shopify_return": shopify_return}


@router.post("/order/exchange")
async def create_exchange_action(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    context = await get_order_action_context(db, payload, current_user)
    if isinstance(context, JSONResponse):
        return context
    order_doc, shopify_cred, membership = context

    note = (payload.get("note") or "").strip()
    if not note:
        return JSONResponse(status_code=400, content={"error": "Exchange note is required"})
    exchange_items = payload.get("exchange_items", [])
    if not exchange_items:
        return JSONResponse(status_code=400, content={"error": "At least one exchange item is required"})

    amount = payload.get("amount") or 0
    try:
        return_line_items = await build_return_line_items(
            payload.get("shop"),
            shopify_cred["access_token"],
            payload.get("order_id"),
            payload.get("selected_items", []),
            note,
        )
        exchange_line_items = [
            {
                "variantId": variant_gid(item.get("variant_id")),
                "quantity": int(item.get("quantity") or 1),
            }
            for item in exchange_items
            if item.get("variant_id")
        ]
        if not exchange_line_items:
            return JSONResponse(status_code=400, content={"error": "Exchange variant ID is required"})
        shopify_return = await create_shopify_return(
            payload.get("shop"),
            shopify_cred["access_token"],
            payload.get("order_id"),
            return_line_items,
            exchange_line_items,
        )
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    await record_order_action_and_audit(
        db,
        order_doc=order_doc,
        current_user=current_user,
        membership=membership,
        action_type="exchange",
        audit_action="Created exchange",
        amount=amount,
        note=note,
        message_id=payload.get("message_id", ""),
        details={
            "selected_items": payload.get("selected_items", []),
            "exchange_items": exchange_items,
            "shopify_return_id": shopify_return.get("id"),
        },
    )
    return {"msg": "Exchange created successfully.", "shopify_return": shopify_return}


@router.post("/order/resend")
async def resend_order_notification(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    context = await get_order_action_context(db, payload, current_user)
    if isinstance(context, JSONResponse):
        return context
    order_doc, shopify_cred, membership = context

    notification_type = payload.get("type")
    if notification_type != "invoice":
        return JSONResponse(status_code=400, content={"error": "Invalid notification type"})

    action_type = "resend_invoice"
    audit_action = "Resent invoice"
    note = (payload.get("note") or "").strip()
    try:
        shopify_order = await send_order_invoice(
            payload.get("shop"),
            shopify_cred["access_token"],
            payload.get("order_id"),
            payload.get("email") or order_doc.get("customer", {}).get("email"),
        )
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    await record_order_action_and_audit(
        db,
        order_doc=order_doc,
        current_user=current_user,
        membership=membership,
        action_type=action_type,
        audit_action=audit_action,
        amount=0,
        note=note,
        message_id=payload.get("message_id", ""),
        details={"shopify_order_id": shopify_order.get("id")},
    )
    return {"msg": "Invoice sent successfully.", "shopify_order": shopify_order}


@router.post("/order/add-note")
async def add_order_note(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    context = await get_order_action_context(db, payload, current_user)
    if isinstance(context, JSONResponse):
        return context
    order_doc, shopify_cred, membership = context

    note = (payload.get("note") or "").strip()
    if not note:
        return JSONResponse(status_code=400, content={"error": "Order note is required"})

    shop = payload.get("shop")
    order_id = payload.get("order_id")
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders/{int(order_id)}.json"
    async with httpx.AsyncClient() as client:
        response = await client.put(
            url,
            headers={
                "X-Shopify-Access-Token": shopify_cred["access_token"],
                "Content-Type": "application/json",
            },
            json={"order": {"id": int(order_id), "note": note}},
        )
    if response.status_code >= 400:
        return JSONResponse(status_code=response.status_code, content=response.json())

    await record_order_action_and_audit(
        db,
        order_doc=order_doc,
        current_user=current_user,
        membership=membership,
        action_type="add_note",
        audit_action="Added order note",
        amount=0,
        note=note,
        message_id=payload.get("message_id", ""),
        order_updates={"note": note},
    )
    return {"msg": "Order note added successfully.", "shopify_response": response.json()}


@router.post("/order/fulfillment-hold")
async def hold_fulfillment(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    context = await get_order_action_context(db, payload, current_user)
    if isinstance(context, JSONResponse):
        return context
    order_doc, shopify_cred, membership = context

    note = (payload.get("note") or "").strip() or "Placed on hold from Attentify"
    reason = payload.get("reason") or "other"
    shop = payload.get("shop")
    order_id = payload.get("order_id")
    fulfillment_orders = await get_fulfillment_orders(shop, shopify_cred["access_token"], order_id)
    if not fulfillment_orders:
        return JSONResponse(status_code=404, content={"error": "No fulfillment orders found"})

    held_ids = []
    async with httpx.AsyncClient() as client:
        for fulfillment_order in fulfillment_orders:
            fulfillment_order_id = fulfillment_order.get("id")
            if not fulfillment_order_id:
                continue
            response = await client.post(
                f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/fulfillment_orders/{fulfillment_order_id}/hold.json",
                headers={
                    "X-Shopify-Access-Token": shopify_cred["access_token"],
                    "Content-Type": "application/json",
                },
                json={"fulfillment_hold": {"reason": reason, "reason_notes": note}},
            )
            if response.status_code < 400:
                held_ids.append(str(fulfillment_order_id))

    if not held_ids:
        return JSONResponse(status_code=400, content={"error": "Failed to hold fulfillment orders"})

    await record_order_action_and_audit(
        db,
        order_doc=order_doc,
        current_user=current_user,
        membership=membership,
        action_type="fulfillment_hold",
        audit_action="Placed fulfillment on hold",
        amount=0,
        note=note,
        message_id=payload.get("message_id", ""),
        details={"fulfillment_order_ids": held_ids, "reason": reason},
        order_updates={"fulfillment_hold_status": "held"},
    )
    return {"msg": "Fulfillment hold placed successfully.", "fulfillment_order_ids": held_ids}


@router.post("/order/fulfillment-release")
async def release_fulfillment_hold(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    context = await get_order_action_context(db, payload, current_user)
    if isinstance(context, JSONResponse):
        return context
    order_doc, shopify_cred, membership = context

    note = (payload.get("note") or "").strip()
    shop = payload.get("shop")
    order_id = payload.get("order_id")
    fulfillment_orders = await get_fulfillment_orders(shop, shopify_cred["access_token"], order_id)
    if not fulfillment_orders:
        return JSONResponse(status_code=404, content={"error": "No fulfillment orders found"})

    released_ids = []
    async with httpx.AsyncClient() as client:
        for fulfillment_order in fulfillment_orders:
            fulfillment_order_id = fulfillment_order.get("id")
            if not fulfillment_order_id:
                continue
            response = await client.post(
                f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/fulfillment_orders/{fulfillment_order_id}/release_hold.json",
                headers={
                    "X-Shopify-Access-Token": shopify_cred["access_token"],
                    "Content-Type": "application/json",
                },
            )
            if response.status_code < 400:
                released_ids.append(str(fulfillment_order_id))

    if not released_ids:
        return JSONResponse(status_code=400, content={"error": "Failed to release fulfillment holds"})

    await record_order_action_and_audit(
        db,
        order_doc=order_doc,
        current_user=current_user,
        membership=membership,
        action_type="fulfillment_release",
        audit_action="Released fulfillment hold",
        amount=0,
        note=note,
        message_id=payload.get("message_id", ""),
        details={"fulfillment_order_ids": released_ids},
        order_updates={"fulfillment_hold_status": "released"},
    )
    return {"msg": "Fulfillment hold released successfully.", "fulfillment_order_ids": released_ids}
