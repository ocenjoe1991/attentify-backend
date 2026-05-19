# app/routes/message.py

from fastapi import APIRouter, HTTPException, Depends, Body, Query
from app.services.gmail_service import fetch_all_gmail_accounts, get_gmail_service
from app.db.mongodb import get_database
from app.models.message import Message, ChatEntry, PyObjectId 
from typing import List
import re
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from app.services.ai_service import analyze_emails_with_ai
import json
from bson import ObjectId
import base64
from email.utils import formatdate, format_datetime
from email.mime.text import MIMEText
from datetime import datetime, timezone
from email.utils import parseaddr
from pymongo import DESCENDING
from app.core.security import get_current_user

from math import ceil

router = APIRouter()

@router.post("/fetch-all")
async def fetch_all(body: dict, db=Depends(get_database), current_user: dict = Depends(get_current_user)):
    company_id = body.get("company_id", "")
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")
    
    result = await fetch_all_gmail_accounts(db, user_id=str(current_user["_id"]), company_id= company_id)
    return {"result": result}

def extract_name(email_str: str) -> str:
    match = re.match(r"^(.*?)\s*<", email_str)
    return match.group(1).strip() if match else email_str

def doc_to_message(doc: dict) -> Message:
    # Clean client
    raw_client = doc.get("client", "")
    cleaned_client = extract_name(raw_client)

    return Message(
        id=PyObjectId(doc['_id']),
        client=cleaned_client,
        agent=doc.get("agent"),
        session_id=doc.get("session_id"),
        started_at=doc.get("started_at"),
        last_updated=doc.get("last_updated"),
        status=doc.get("status", "open"),
        channel=doc.get("channel"),
        title=doc.get("title"),
        ai_summary=doc.get("ai_summary"),
        tags=doc.get("tags", []),
        resolved_by_ai=doc.get("resolved_by_ai", False),
    )

def doc_to_message_detail(doc: dict) -> Message:
    return Message(
        id=doc["_id"],
        client=extract_name(doc.get("client", "")),
        agent=doc.get("agent"),
        session_id=doc.get("session_id"),
        started_at=doc.get("started_at"),
        last_updated=doc.get("last_updated"),
        status=doc.get("status", "open"),
        channel=doc.get("channel"),
        title=doc.get("title"),
        ai_summary=doc.get("ai_summary"),
        tags=doc.get("tags", []),
        resolved_by_ai=doc.get("resolved_by_ai", False),
        messages=[]  # or omit this line if optional in schema
    )

@router.get("/", response_model=List[dict])
async def get_messages(db=Depends(get_database), current_user: dict = Depends(get_current_user)):
    cursor = db["messages"].find({"user_id": current_user["_id"]}).sort("last_updated", DESCENDING)
    messages = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"]) 
        doc["user_id"] = str(doc["user_id"])
        doc["company_id"] = str(doc["company_id"])
        raw_client = doc.get("client", "")
        cleaned_client = extract_name(raw_client)
        doc["client"] = cleaned_client

        # Assigned member
        member = None
        assigned_member_id = doc.get("assigned_member_id")
        if assigned_member_id:
            try:
                member_obj = await db["users"].find_one({"_id": assigned_member_id if isinstance(assigned_member_id, ObjectId) else ObjectId(assigned_member_id)})
                if member_obj:
                    member_obj["_id"] = str(member_obj["_id"])
                    # Include only desired member fields
                    member = {
                        "id": member_obj["_id"],
                        "name": f"{member_obj.get('first_name', '')} {member_obj.get('last_name', '')}".strip(),
                        "email": member_obj.get("email", "")
                    }
            except Exception:
                member = None
        doc["assigned_to"] = member
        if "assigned_member_id" in doc and doc["assigned_member_id"]:
            doc.pop("assigned_member_id", None)
        doc.pop("messages", None)
        messages.append(doc)
    return messages

@router.get("/company_messages", response_model=dict)
async def get_company_messages(
    company_id: str = Query(..., description="ID of the company"),
    search: str = Query("", description="Search by message title or client name/email"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Page size"),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: dict = Depends(get_current_user)
):
    if not ObjectId.is_valid(company_id):
        raise HTTPException(status_code=400, detail="Invalid company ID")

    # ✅ Verify membership
    membership = await db["memberships"].find_one(
        {"user_id": current_user["_id"], "company_id": ObjectId(company_id)}
    )
    if not membership:
        raise HTTPException(status_code=403, detail="User is not a member of this company")

    role = membership.get("role")

    # ✅ Base query depending on role
    query = {"company_id": ObjectId(company_id)}
    if role == "store_owner":
        query["user_id"] = current_user["_id"]
    elif role == "agent":
        query["assigned_member_id"] = current_user["_id"]
    elif role not in ["company_owner", "store_owner", "agent"]:
        query["user_id"] = current_user["_id"]

    # ✅ Apply search filter (case-insensitive)
    if search.strip():
        search_regex = {"$regex": search.strip(), "$options": "i"}
        query["$or"] = [
            {"title": search_regex},
            {"client": search_regex},
        ]

    # Count total documents for pagination
    total_count = await db["messages"].count_documents(query)
    totalPages = ceil(total_count / size)

    # ✅ Pagination
    skip = (page - 1) * size

    cursor = (
        db["messages"]
        .find(query)
        .sort("last_updated", DESCENDING)
        .skip(skip)
        .limit(size)
    )

    messages = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        doc["user_id"] = str(doc["user_id"])
        doc["company_id"] = str(doc["company_id"])

        # ✅ Clean client name
        raw_client = doc.get("client", "")
        doc["client"] = extract_name(raw_client)

        # ✅ Get assigned member details
        assigned_member_id = doc.get("assigned_member_id")
        member = None
        if assigned_member_id:
            try:
                member_obj = await db["users"].find_one(
                    {"_id": assigned_member_id if isinstance(assigned_member_id, ObjectId) else ObjectId(assigned_member_id)}
                )
                if member_obj:
                    member = {
                        "id": str(member_obj["_id"]),
                        "name": f"{member_obj.get('first_name', '')} {member_obj.get('last_name', '')}".strip(),
                        "email": member_obj.get("email", "")
                    }
            except Exception:
                member = None
        doc["assigned_to"] = member

        # ✅ Cleanup unused fields
        doc.pop("assigned_member_id", None)
        doc.pop("messages", None)
        doc.pop("comments", None)

        messages.append(doc)

    return {
        "messages": messages,
        "totalPages": totalPages
    }

@router.get("/{id}", response_model=dict)
async def get_message(id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    doc = await db["messages"].find_one({"_id": ObjectId(id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Message not found")

    # Convert ObjectIds → strings
    doc["_id"] = str(doc["_id"])
    doc["user_id"] = str(doc["user_id"])
    doc["company_id"] = str(doc["company_id"])
    if "assigned_member_id" in doc and doc["assigned_member_id"]:
        doc["assigned_member_id"] = str(doc["assigned_member_id"])

    # Properly await comment serialization
    comments = []
    for c in doc.get("comments", []):
        comments.append(await serialize_comment(c, db))
    doc["comments"] = comments

    return doc

@router.put("/{id}", response_model=dict)
async def update_message(id: str, payload: dict = Body(...), db: AsyncIOMotorDatabase = Depends(get_database)):
    db["messages"].find_one_and_update(
        {"_id": ObjectId(id)},
        {"$set": payload}
    )
    return {"message": "Message updated"}

async def serialize_comment(comment: dict, db) -> dict:
    user = await db["users"].find_one({"_id": comment["user_id"]})
    return {
        "id": str(comment["_id"]),
        "user_id": str(comment["user_id"]),  # raw user reference
        "user": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() if user else None,
        "content": comment["content"],
        "status": comment.get("status"),
        "edited": comment.get("edited"),
        "created_at": comment["created_at"].strftime("%Y-%m-%d %H:%M:%S") if comment.get("created_at") else None,
        "updated_at": comment["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if comment.get("updated_at") else None,
    }

@router.post("/add_comment/{message_id}", response_model=dict)
async def add_comment(
    message_id: str,
    payload: dict = Body(...),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    content = payload.get("content")
    status = payload.get("status", "Pending")

    # Build new comment object
    new_comment = {
        "_id": ObjectId(),  # unique ID for comment
        "user_id": ObjectId(user["_id"]),
        "content": content,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "status": status
    }

    # Push comment into the message's comments array
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$push": {"comments": new_comment}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"message": "Comment added", "comment": await serialize_comment(new_comment, db)}

@router.put("/edit_comment/{message_id}/{comment_id}", response_model=dict)
async def edit_comment(
    message_id: str,
    comment_id: str,
    content: str = Body(..., embed=True),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not (ObjectId.is_valid(message_id) and ObjectId.is_valid(comment_id)):
        raise HTTPException(status_code=400, detail="Invalid IDs")
    
    # Find and update comment inside array
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id), "comments._id": ObjectId(comment_id)},
        {
            "$set": {
                "comments.$.content": content,
                "comments.$.edited": True,
                "comments.$.updated_at": datetime.utcnow()
            }
        }
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")

    # Get updated comment
    message = await db["messages"].find_one({"_id": ObjectId(message_id)})
    updated_comment = next((c for c in message["comments"] if c["_id"] == ObjectId(comment_id)), None)

    return {"message": "Comment updated", "comment": await serialize_comment(updated_comment, db)}

@router.put("/approve_comment/{message_id}/{comment_id}", response_model=dict)
async def approve_comment(
    message_id: str,
    comment_id: str,
    status: str = Body(..., embed=True),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not (ObjectId.is_valid(message_id) and ObjectId.is_valid(comment_id)):
        raise HTTPException(status_code=400, detail="Invalid IDs")
    
    # Find and update comment inside array
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id), "comments._id": ObjectId(comment_id)},
        {
            "$set": {
                "comments.$.status": status,
                "comments.$.updated_at": datetime.utcnow()
            }
        }
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")

    # Get updated comment
    message = await db["messages"].find_one({"_id": ObjectId(message_id)})
    updated_comment = next((c for c in message["comments"] if c["_id"] == ObjectId(comment_id)), None)

    return {"message": "Comment approved", "comment": await serialize_comment(updated_comment, db)}

# --- Delete Comment ---
@router.delete("/delete_comment/{message_id}/{comment_id}", response_model=dict)
async def delete_comment(
    message_id: str,
    comment_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not (ObjectId.is_valid(message_id) and ObjectId.is_valid(comment_id)):
        raise HTTPException(status_code=400, detail="Invalid IDs")

    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$pull": {"comments": {"_id": ObjectId(comment_id)}}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")

    return {"message": "Comment deleted"}

@router.patch("/{message_id}")
async def update_message_field(
    message_id: str,
    body: dict = Body(...), 
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    field = body.get("field")
    value = body.get("value")

    if not field:
        raise HTTPException(status_code=400, detail="Field is required")

    # Optionally, prevent updates to _id or forbidden fields
    if field == "_id":
        raise HTTPException(status_code=400, detail="Cannot update _id field")
    
    # Convert to ObjectId where needed
    if field == "assigned_member_id" and value:
        try:
            value = ObjectId(value)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid assigned_member_id")

    # Perform update
    result = await db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$set": {field: value}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"message": f"{field} updated"}

def clean_json_response(response: str):
    """
    Cleans a model-generated JSON response by removing code fences and extra text.
    Returns a parsed Python dict.
    """
    if not response:
        return {}

    # Remove common Markdown code fences like ```json ... ```
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", response.strip())

    # Extract JSON object if surrounded by text accidentally
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON response: {e}\nRaw text: {response}")
    
@router.post("/analyze_as_list", response_model=list)
async def analyze_email_message_as_list(
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Analyze all email ChatEntry objects in a message and extract order/refund/cancel info as JSON.
    Input: JSON body with { "message_id": str }.
    Output: List of JSON results, one per ChatEntry.
    """
    message_id = body.get("message_id")
    if not message_id or not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    doc = await db["messages"].find_one({"_id": ObjectId(message_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Message not found")

    result = await analyze_emails_with_ai(doc)
    order_list = []
    for entry in result:
        try:
            order_info = json.loads(entry["response"])
            if order_info.get("order_id") and order_info.get("status") == 1:
                order_info["shopify_order"] = {}
                order_list.append(order_info)
        except Exception:
            continue

    return order_list

@router.post("/analyze", response_model=dict)
async def analyze_email_message(
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Analyze the last three email ChatEntry objects in a message and extract order/refund/cancel info as JSON.
    Input: JSON body with { "message_id": str }.
    Output: Single JSON result for the combined analysis.
    """
    message_id = body.get("message_id")
    if not message_id or not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=400, detail="Invalid message ID")

    message_doc = await db["messages"].find_one({"_id": ObjectId(message_id)})
    if not message_doc:
        raise HTTPException(status_code=404, detail="Message not found")

    if not (order_info := message_doc.get('order_info')):
        result = await analyze_emails_with_ai(message_doc)
        # result is now a single dict, not a list
        
        response = getattr(result, 'content', str(result))
        print("Email AI process response: ", response)
        order_info = clean_json_response(response)

        if (order_info.get('order_id')):
            await db["messages"].update_one(
                {"_id": message_doc["_id"]},
                {
                    "$set": {
                        "order_info": order_info,
                    }
                }
            )
    
    order_id = str(order_info.get("order_id", ""))
    order_name = order_id if order_id.startswith("#") else "#" + order_id

    db_order = await db["orders"].find_one({"name": order_name})
    
    if db_order:
        match = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', message_doc.get('client'))
        email = match[0]

        if (db_order.get("customer", {}).get("email", "") == email):
            db_order["_id"] = str(db_order["_id"])
            db_order["user_id"] = str(db_order.get("user_id", ""))
            db_order["company_id"] = str(db_order.get("company_id", ""))
            order_info["shopify_order"] = db_order

        else:
            order_info["msg"] = "Email not matched"

    else:
        order_info["msg"] = "Order not found"

    return order_info

@router.post("/{id}/reply", response_model=dict)
async def reply_to_message(
    id: str,
    body: dict = Body(...),  # expects: { "content": "the reply text" }
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """
    Reply to a message by adding a new ChatEntry and sending email via Gmail API.
    Input: Message ID (path) and reply content (body).
    Output: Updated message document.
    """
    
    if not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid message ID")
    
    message = await db["messages"].find_one({"_id": ObjectId(id)})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    
    # Find latest client message for threading
    client_message = None
    for msg in reversed(message.get("messages", [])):
        if msg.get("sender") == message.get("client"):
            client_message = msg
            break
    if not client_message:
        raise HTTPException(status_code=400, detail="No client message to reply to.")
    
    # Identify Gmail user (agent sending reply)
    agent_id = None
    agent_id = message.get("agent")  # agent_id should be the email of the agent

    if not agent_id:
        raise HTTPException(status_code=400, detail="No Gmail user found in participants.")

    _, agent_email = parseaddr(agent_id)
    user_creds = await db["gmail_accounts"].find_one({"email": agent_email})
    if not user_creds:
        raise HTTPException(status_code=400, detail="User Gmail credentials not found.")

    thread_id = message.get("thread_id")
    subject = client_message.get("title", "No Subject")
    original_msg_id = client_message.get("metadata", {}).get("gmail_id")
    to_addr = message.get("client")  # recipient (client)

    if original_msg_id and not original_msg_id.startswith("<"):
        original_msg_id = f"<{original_msg_id}>"

    mime_msg = MIMEText(body["content"], "html")
    mime_msg['To'] = to_addr
    mime_msg['From'] = agent_email
    mime_msg['Subject'] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original_msg_id:
        mime_msg['In-Reply-To'] = original_msg_id
        mime_msg['References'] = original_msg_id
    mime_msg['Date'] = formatdate(localtime=True)

    raw_message = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    
    # Send via Gmail API
    service = get_gmail_service(user_creds)
    sent = service.users().messages().send(
        userId="me",
        body={
            'raw': raw_message,
            'threadId': thread_id
        }
    ).execute()

    # Construct ChatEntry and save to DB
    now = datetime.now(timezone.utc).astimezone()
    reply_entry = {
        "sender": agent_email,
        "recipient": to_addr,
        "content": body["content"],
        "title": subject if subject.lower().startswith("re:") else f"Re: {subject}",
        "timestamp": datetime.utcnow(),
        "message_type": "html",
        "channel": "email",
        "metadata": {
            "gmail_id": sent.get("id"),
            "from": agent_email,
            "to": to_addr,
            "date": format_datetime(now)
        }
    }

    await db["messages"].update_one(
        {"_id": ObjectId(id)},
        {
            "$push": {"messages": reply_entry},
            "$set": {"last_updated": reply_entry["timestamp"]}
        }
    )

    updated_message = await db["messages"].find_one({"_id": ObjectId(id)})

    if '_id' in updated_message:
        updated_message['_id'] = str(updated_message['_id'])
        updated_message['user_id'] = str(updated_message['user_id'])
        updated_message['company_id'] = str(updated_message['company_id'])
    return updated_message