from dotenv import load_dotenv
load_dotenv()

import os
import re
import uuid
import secrets
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import base64
from pathlib import Path

import bcrypt
import httpx
import jwt
from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
MEDIA_TYPES = {"text", "image", "document", "audio", "video"}
STATUSES = {"open", "pending", "resolved"}
EVO_URL = os.environ.get("EVOLUTION_API_URL", "").rstrip("/")
EVO_KEY = os.environ.get("EVOLUTION_API_KEY", "")
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Slash API")
api_router = APIRouter(prefix="/api")
security = HTTPBearer(auto_error=False)
logger = logging.getLogger("slash")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_token(user: dict) -> str:
    payload = {
        "sub": user["id"],
        "tenant_id": user["tenant_id"],
        "role": user["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def public_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k not in ("password_hash", "_id")}


def public_tenant(tenant: dict) -> dict:
    return {k: v for k, v in tenant.items() if k != "_id"}


async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(401, "User not found")
    return user


async def require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return user


# ---------- Input models ----------

class SignupInput(BaseModel):
    company_name: str = Field(min_length=2)
    admin_name: str = Field(min_length=2)
    email: EmailStr
    password: str = Field(min_length=6)
    evolution_instance_name: Optional[str] = ""
    marketly_instance_id: Optional[str] = ""
    marketly_bearer_token: Optional[str] = ""


class SendTextInput(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class SendLinkInput(BaseModel):
    url: str = Field(min_length=8)
    caption: Optional[str] = ""


class TenantPatch(BaseModel):
    company_name: Optional[str] = None
    evolution_instance_name: Optional[str] = None


class LoginInput(BaseModel):
    email: EmailStr
    password: str


class InviteInput(BaseModel):
    name: str = Field(min_length=2)
    email: EmailStr
    password: str = Field(min_length=6)
    role: str = "agent"


class ConversationPatch(BaseModel):
    status: Optional[str] = None
    assigned_to: Optional[str] = None


class NoteInput(BaseModel):
    body: str = Field(min_length=1)


class ContactPatch(BaseModel):
    name: Optional[str] = None
    labels: Optional[List[str]] = None
    notes: Optional[str] = None


class RolePatch(BaseModel):
    role: str


# ---------- Auth ----------

@api_router.post("/auth/signup")
async def signup(data: SignupInput):
    email = data.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(409, "Email already registered")
    tenant = {
        "id": str(uuid.uuid4()),
        "company_name": data.company_name,
        "evolution_instance_name": (data.evolution_instance_name or "").strip(),
        "marketly_instance_id": data.marketly_instance_id or "",
        "marketly_bearer_token": data.marketly_bearer_token or "",
        "api_key": "sk_" + secrets.token_hex(20),
        "created_at": utcnow(),
    }
    await db.tenants.insert_one(tenant)
    user = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant["id"],
        "name": data.admin_name,
        "email": email,
        "password_hash": hash_password(data.password),
        "role": "admin",
        "created_at": utcnow(),
    }
    await db.users.insert_one(user)
    return {"token": create_token(user), "user": public_user(user), "tenant": public_tenant(tenant)}


@api_router.post("/auth/login")
async def login(data: LoginInput):
    email = data.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]})
    return {"token": create_token(user), "user": public_user(user), "tenant": public_tenant(tenant)}


@api_router.get("/auth/me")
async def me(user=Depends(get_current_user)):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]})
    return {"user": public_user(user), "tenant": public_tenant(tenant)}


# ---------- Evolution API client ----------

MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
}


def save_upload_bytes(content: bytes, filename: str, mimetype: str = "") -> str:
    ext = Path(filename or "").suffix or MIME_EXT.get((mimetype or "").split(";")[0].strip(), ".bin")
    fname = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / fname).write_bytes(content)
    return f"/api/files/{fname}"


def save_base64_media(b64: str, filename: str, mimetype: str = ""):
    if not b64:
        return None
    if b64.startswith("data:") and "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        content = base64.b64decode(b64)
    except Exception:
        return None
    return save_upload_bytes(content, filename, mimetype)


async def evolution_request(method: str, path: str, payload: dict = None, timeout: float = 90.0) -> dict:
    if not EVO_URL or not EVO_KEY:
        raise HTTPException(500, "Evolution API is not configured on the server")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30.0)) as client:
            resp = await client.request(
                method,
                f"{EVO_URL}{path}",
                headers={"apikey": EVO_KEY, "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.RequestError as e:
        raise HTTPException(400, f"Evolution API request failed ({type(e).__name__}). The instance may be offline or the server is cold-starting — try again.")
    if resp.status_code >= 400:
        raise HTTPException(400, f"Evolution API error ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


# ---------- Webhook ingestion ----------

def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize_events(payload) -> list:
    events = []
    if not isinstance(payload, dict):
        return events
    # WhatsApp Cloud API / Marketly nested format
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {}
            for c in value.get("contacts") or []:
                names[c.get("wa_id")] = (c.get("profile") or {}).get("name")
            for msg in value.get("messages") or []:
                mtype = (msg.get("type") or "text").lower()
                media = msg.get(mtype) if isinstance(msg.get(mtype), dict) else {}
                phone = msg.get("from")
                events.append({
                    "phone": phone,
                    "name": names.get(phone),
                    "type": mtype if mtype in MEDIA_TYPES else "text",
                    "text": media.get("body") or (msg.get("text") if isinstance(msg.get("text"), str) else None),
                    "media_url": _first(media, "link", "url"),
                    "caption": media.get("caption"),
                    "filename": media.get("filename"),
                    "external_id": msg.get("id"),
                    "timestamp": msg.get("timestamp"),
                })
    # Evolution API messages.upsert format
    evo = payload.get("data")
    if isinstance(evo, dict) and isinstance(evo.get("key"), dict):
        key = evo["key"]
        msg = evo.get("message") or {}
        remote = key.get("remoteJid") or ""
        media_map = {
            "imageMessage": "image", "videoMessage": "video",
            "audioMessage": "audio", "documentMessage": "document",
            "documentWithCaptionMessage": "document",
        }
        mk = next((k for k in media_map if k in msg), None)
        media_obj = (msg.get(mk) or {}) if mk else {}
        if mk == "documentWithCaptionMessage":
            media_obj = media_obj.get("documentMessage") or media_obj
        text = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
        events.append({
            "phone": remote.split("@")[0],
            "name": evo.get("pushName"),
            "type": media_map.get(mk, "text"),
            "text": text,
            "media_url": _first(media_obj, "url", "mediaUrl"),
            "media_base64": msg.get("base64") or media_obj.get("base64"),
            "caption": media_obj.get("caption"),
            "filename": media_obj.get("fileName"),
            "mimetype": media_obj.get("mimetype"),
            "external_id": key.get("id"),
            "timestamp": evo.get("messageTimestamp"),
            "from_me": bool(key.get("fromMe")),
        })
        return events

    # Flat / simple format
    flat = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    phone = _first(flat, "phone", "from", "from_number", "wa_id", "sender")
    if phone and not events:
        mtype = str(_first(flat, "type", "message_type", "msg_type") or "text").lower()
        events.append({
            "phone": str(phone),
            "name": _first(flat, "name", "contact_name", "pushname", "sender_name"),
            "type": mtype if mtype in MEDIA_TYPES else "text",
            "text": _first(flat, "text", "body", "message", "content"),
            "media_url": _first(flat, "media_url", "media", "file_url", "url", "link"),
            "caption": _first(flat, "caption"),
            "filename": _first(flat, "filename", "file_name"),
            "external_id": _first(flat, "message_id", "msg_id", "id"),
            "timestamp": _first(flat, "timestamp", "created_at", "time"),
        })
    return events


async def store_inbound(tenant_id: str, ev: dict):
    if not ev.get("phone"):
        return None
    if ev.get("external_id"):
        dup = await db.messages.find_one({"tenant_id": tenant_id, "external_id": ev["external_id"]})
        if dup:
            return None
    created = utcnow()
    ts = ev.get("timestamp")
    if ts:
        try:
            created = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            pass
    phone = re.sub(r"\D", "", str(ev["phone"])) or str(ev["phone"])
    from_me = bool(ev.get("from_me"))
    media_url = ev.get("media_url")
    if ev.get("media_base64"):
        media_url = save_base64_media(ev["media_base64"], ev.get("filename") or ev["type"], ev.get("mimetype")) or media_url
    contact = await db.contacts.find_one({"tenant_id": tenant_id, "phone": phone})
    if not contact:
        contact = {
            "id": str(uuid.uuid4()), "tenant_id": tenant_id, "phone": phone,
            "name": ev.get("name") or phone, "labels": [], "notes": "", "created_at": created,
        }
        await db.contacts.insert_one(contact)
    elif ev.get("name") and contact.get("name") == phone:
        await db.contacts.update_one({"id": contact["id"]}, {"$set": {"name": ev["name"]}})
        contact["name"] = ev["name"]
    conv = await db.conversations.find_one({"tenant_id": tenant_id, "contact_id": contact["id"]})
    if not conv:
        conv = {
            "id": str(uuid.uuid4()), "tenant_id": tenant_id, "contact_id": contact["id"],
            "status": "open", "assigned_to": None, "unread_count": 0, "created_at": created,
        }
        await db.conversations.insert_one(conv)
    msg = {
        "id": str(uuid.uuid4()), "tenant_id": tenant_id, "conversation_id": conv["id"],
        "contact_id": contact["id"], "direction": "outbound" if from_me else "inbound",
        "is_note": False, "type": ev["type"], "text": ev.get("text"),
        "media_url": media_url, "caption": ev.get("caption"), "filename": ev.get("filename"),
        "mimetype": ev.get("mimetype"), "external_id": ev.get("external_id"),
        "author_name": ev.get("name") if from_me else None, "created_at": created,
    }
    await db.messages.insert_one(msg)
    preview = ev.get("text") or ev.get("caption") or f"[{ev['type'].capitalize()}]"
    if from_me:
        preview = f"You: {preview}"
    new_status = "open" if (conv.get("status") == "resolved" and not from_me) else conv.get("status", "open")
    update = {
        "$set": {
            "last_message_preview": preview[:140],
            "last_message_at": created,
            "last_message_type": ev["type"],
            "status": new_status,
            "updated_at": utcnow(),
        }
    }
    if not from_me:
        update["$inc"] = {"unread_count": 1}
    await db.conversations.update_one({"id": conv["id"]}, update)
    msg.pop("_id", None)
    return msg


@api_router.post("/webhook/inbound/{tenant_id}")
async def inbound_webhook(tenant_id: str, request: Request):
    tenant = await db.tenants.find_one({"id": tenant_id})
    if not tenant:
        raise HTTPException(404, "Unknown tenant")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    events = normalize_events(payload)
    stored = []
    for ev in events:
        msg = await store_inbound(tenant_id, ev)
        if msg:
            stored.append(msg)
    return {"status": "ok", "received": len(stored)}


# ---------- Conversations ----------

async def tenant_users_map(tenant_id: str):
    users = await db.users.find({"tenant_id": tenant_id}, {"_id": 0, "password_hash": 0}).to_list(500)
    return {u["id"]: u for u in users}


@api_router.get("/conversations")
async def list_conversations(status: Optional[str] = None, search: Optional[str] = None, user=Depends(get_current_user)):
    q = {"tenant_id": user["tenant_id"]}
    if status in STATUSES:
        q["status"] = status
    convs = await db.conversations.find(q, {"_id": 0}).sort("last_message_at", -1).to_list(500)
    contacts = {c["id"]: c for c in await db.contacts.find({"tenant_id": user["tenant_id"]}, {"_id": 0}).to_list(5000)}
    users = await tenant_users_map(user["tenant_id"])
    out = []
    for c in convs:
        contact = contacts.get(c["contact_id"], {})
        item = dict(c)
        item["contact_name"] = contact.get("name")
        item["contact_phone"] = contact.get("phone")
        item["contact_labels"] = contact.get("labels", [])
        item["assignee_name"] = (users.get(c.get("assigned_to")) or {}).get("name")
        if search:
            s = search.lower()
            hay = " ".join([
                contact.get("name") or "", contact.get("phone") or "",
                c.get("last_message_preview") or "",
            ]).lower()
            if s not in hay:
                continue
        out.append(item)
    return out


@api_router.get("/conversations/{cid}")
async def get_conversation(cid: str, user=Depends(get_current_user)):
    conv = await db.conversations.find_one({"id": cid, "tenant_id": user["tenant_id"]}, {"_id": 0})
    if not conv:
        raise HTTPException(404, "Conversation not found")
    contact = await db.contacts.find_one({"id": conv["contact_id"]}, {"_id": 0})
    users = await tenant_users_map(user["tenant_id"])
    conv["contact"] = contact
    conv["assignee_name"] = (users.get(conv.get("assigned_to")) or {}).get("name")
    return conv


@api_router.get("/conversations/{cid}/messages")
async def get_messages(cid: str, user=Depends(get_current_user)):
    conv = await db.conversations.find_one({"id": cid, "tenant_id": user["tenant_id"]})
    if not conv:
        raise HTTPException(404, "Conversation not found")
    await db.conversations.update_one({"id": cid}, {"$set": {"unread_count": 0}})
    return await db.messages.find(
        {"conversation_id": cid, "tenant_id": user["tenant_id"]}, {"_id": 0}
    ).sort("created_at", 1).to_list(2000)


@api_router.patch("/conversations/{cid}")
async def update_conversation(cid: str, data: ConversationPatch, user=Depends(get_current_user)):
    conv = await db.conversations.find_one({"id": cid, "tenant_id": user["tenant_id"]})
    if not conv:
        raise HTTPException(404, "Conversation not found")
    updates = {"updated_at": utcnow()}
    if data.status is not None:
        if data.status not in STATUSES:
            raise HTTPException(400, "Invalid status")
        updates["status"] = data.status
    if data.assigned_to is not None or "assigned_to" in data.model_fields_set:
        if data.assigned_to:
            member = await db.users.find_one({"id": data.assigned_to, "tenant_id": user["tenant_id"]})
            if not member:
                raise HTTPException(400, "Assignee not in tenant")
        updates["assigned_to"] = data.assigned_to or None
    await db.conversations.update_one({"id": cid}, {"$set": updates})
    return await get_conversation(cid, user)


@api_router.post("/conversations/{cid}/notes")
async def add_note(cid: str, data: NoteInput, user=Depends(get_current_user)):
    conv = await db.conversations.find_one({"id": cid, "tenant_id": user["tenant_id"]})
    if not conv:
        raise HTTPException(404, "Conversation not found")
    note = {
        "id": str(uuid.uuid4()), "tenant_id": user["tenant_id"], "conversation_id": cid,
        "contact_id": conv["contact_id"], "direction": "internal", "is_note": True,
        "type": "note", "text": data.body, "author_id": user["id"],
        "author_name": user["name"], "created_at": utcnow(),
    }
    await db.messages.insert_one(note)
    note.pop("_id", None)
    return note


# ---------- Outbound messaging (Evolution API) ----------

async def resolve_conversation_context(cid: str, user: dict):
    conv = await db.conversations.find_one({"id": cid, "tenant_id": user["tenant_id"]})
    if not conv:
        raise HTTPException(404, "Conversation not found")
    contact = await db.contacts.find_one({"id": conv["contact_id"]})
    tenant = await db.tenants.find_one({"id": user["tenant_id"]})
    instance = (tenant or {}).get("evolution_instance_name")
    if not instance:
        raise HTTPException(400, "No Evolution instance configured. Set the instance name in Settings.")
    return conv, contact, instance


async def record_outbound(user, conv, contact, mtype, text=None, media_url=None, caption=None, filename=None, mimetype=None, external_id=None):
    msg = {
        "id": str(uuid.uuid4()), "tenant_id": user["tenant_id"], "conversation_id": conv["id"],
        "contact_id": contact["id"], "direction": "outbound", "is_note": False,
        "type": mtype, "text": text, "media_url": media_url, "caption": caption,
        "filename": filename, "mimetype": mimetype, "external_id": external_id,
        "author_id": user["id"], "author_name": user["name"], "created_at": utcnow(),
    }
    await db.messages.insert_one(msg)
    preview = f"You: {text or caption or f'[{mtype.capitalize()}]'}"
    await db.conversations.update_one({"id": conv["id"]}, {"$set": {
        "last_message_preview": preview[:140], "last_message_at": msg["created_at"],
        "last_message_type": mtype, "updated_at": utcnow()}})
    msg.pop("_id", None)
    return msg


@api_router.post("/conversations/{cid}/send")
async def send_text_message(cid: str, data: SendTextInput, user=Depends(get_current_user)):
    conv, contact, instance = await resolve_conversation_context(cid, user)
    number = re.sub(r"\D", "", contact["phone"])
    resp = await evolution_request("POST", f"/message/sendText/{instance}", {
        "number": number, "text": data.text, "delay": 0, "linkPreview": True,
    }, timeout=25.0)
    return await record_outbound(user, conv, contact, "text", text=data.text, external_id=(resp.get("key") or {}).get("id"))


@api_router.post("/conversations/{cid}/send-file")
async def send_file_message(cid: str, file: UploadFile = File(...), caption: str = Form(""), user=Depends(get_current_user)):
    conv, contact, instance = await resolve_conversation_context(cid, user)
    content = await file.read()
    if len(content) > 16 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 16 MB)")
    mime = (file.content_type or "application/octet-stream").split(";")[0]
    fname = file.filename or "file"
    saved_url = save_upload_bytes(content, fname, mime)
    b64 = base64.b64encode(content).decode()
    number = re.sub(r"\D", "", contact["phone"])
    if mime.startswith("audio/"):
        resp = await evolution_request("POST", f"/message/sendWhatsAppAudio/{instance}", {"number": number, "audio": b64}, timeout=30.0)
        mtype = "audio"
    else:
        mediatype = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "document"
        resp = await evolution_request("POST", f"/message/sendMedia/{instance}", {
            "number": number, "mediatype": mediatype, "mimetype": mime,
            "media": b64, "fileName": fname, "caption": caption or "",
        }, timeout=30.0)
        mtype = mediatype
    return await record_outbound(user, conv, contact, mtype, media_url=saved_url, caption=caption or None, filename=fname, mimetype=mime, external_id=(resp.get("key") or {}).get("id"))


@api_router.post("/conversations/{cid}/send-link")
async def send_link_message(cid: str, data: SendLinkInput, user=Depends(get_current_user)):
    conv, contact, instance = await resolve_conversation_context(cid, user)
    number = re.sub(r"\D", "", contact["phone"])
    path = data.url.split("?")[0].lower()
    ext = Path(path).suffix
    fname = Path(path).name or "file"
    img = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    vid = {".mp4", ".mov", ".webm", ".3gp"}
    aud = {".mp3", ".ogg", ".opus", ".m4a", ".wav"}
    if ext in aud:
        resp = await evolution_request("POST", f"/message/sendWhatsAppAudio/{instance}", {"number": number, "audio": data.url}, timeout=30.0)
        mtype, mime = "audio", None
    else:
        mediatype = "image" if ext in img else "video" if ext in vid else "document"
        if mediatype == "document":
            mime = {".pdf": "application/pdf", ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".csv": "text/csv", ".txt": "text/plain", ".zip": "application/zip"}.get(ext, "application/octet-stream")
        else:
            mime = f"{mediatype}/{ext.lstrip('.').replace('jpg', 'jpeg')}"
        resp = await evolution_request("POST", f"/message/sendMedia/{instance}", {
            "number": number, "mediatype": mediatype, "mimetype": mime,
            "media": data.url, "fileName": fname, "caption": data.caption or "",
        }, timeout=30.0)
        mtype = mediatype
    return await record_outbound(user, conv, contact, mtype, media_url=data.url, caption=data.caption or None, filename=fname, mimetype=mime, external_id=(resp.get("key") or {}).get("id"))


# ---------- Contacts ----------

@api_router.get("/contacts")
async def list_contacts(search: Optional[str] = None, user=Depends(get_current_user)):
    contacts = await db.contacts.find({"tenant_id": user["tenant_id"]}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    pipeline = [
        {"$match": {"tenant_id": user["tenant_id"]}},
        {"$group": {"_id": "$contact_id", "count": {"$sum": 1}}},
    ]
    counts = {doc["_id"]: doc["count"] async for doc in db.conversations.aggregate(pipeline)}
    out = []
    for c in contacts:
        item = dict(c)
        item["conversation_count"] = counts.get(c["id"], 0)
        if search:
            s = search.lower()
            hay = " ".join([c.get("name") or "", c.get("phone") or "", " ".join(c.get("labels") or [])]).lower()
            if s not in hay:
                continue
        out.append(item)
    return out


@api_router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: str, data: ContactPatch, user=Depends(get_current_user)):
    contact = await db.contacts.find_one({"id": contact_id, "tenant_id": user["tenant_id"]})
    if not contact:
        raise HTTPException(404, "Contact not found")
    updates = {}
    if data.name is not None:
        updates["name"] = data.name.strip() or contact["phone"]
    if data.labels is not None:
        updates["labels"] = [l.strip() for l in data.labels if l.strip()][:12]
    if data.notes is not None:
        updates["notes"] = data.notes
    if updates:
        await db.contacts.update_one({"id": contact_id}, {"$set": updates})
    return await db.contacts.find_one({"id": contact_id}, {"_id": 0})


# ---------- Team ----------

@api_router.get("/team")
async def list_team(user=Depends(get_current_user)):
    members = await db.users.find({"tenant_id": user["tenant_id"]}, {"_id": 0, "password_hash": 0}).sort("created_at", 1).to_list(500)
    pipeline = [
        {"$match": {"tenant_id": user["tenant_id"], "assigned_to": {"$ne": None}}},
        {"$group": {"_id": "$assigned_to", "count": {"$sum": 1}}},
    ]
    counts = {doc["_id"]: doc["count"] async for doc in db.conversations.aggregate(pipeline)}
    for m in members:
        m["assigned_count"] = counts.get(m["id"], 0)
    return members


@api_router.post("/team/invite")
async def invite_member(data: InviteInput, admin=Depends(require_admin)):
    if data.role not in ("admin", "agent"):
        raise HTTPException(400, "Role must be admin or agent")
    email = data.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(409, "Email already registered")
    member = {
        "id": str(uuid.uuid4()), "tenant_id": admin["tenant_id"], "name": data.name,
        "email": email, "password_hash": hash_password(data.password),
        "role": data.role, "created_at": utcnow(),
    }
    await db.users.insert_one(member)
    return public_user(member)


@api_router.patch("/team/{user_id}")
async def update_member_role(user_id: str, data: RolePatch, admin=Depends(require_admin)):
    if data.role not in ("admin", "agent"):
        raise HTTPException(400, "Role must be admin or agent")
    member = await db.users.find_one({"id": user_id, "tenant_id": admin["tenant_id"]})
    if not member:
        raise HTTPException(404, "Member not found")
    await db.users.update_one({"id": user_id}, {"$set": {"role": data.role}})
    return public_user({**member, "role": data.role})


@api_router.delete("/team/{user_id}")
async def remove_member(user_id: str, admin=Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You cannot remove yourself")
    result = await db.users.delete_one({"id": user_id, "tenant_id": admin["tenant_id"]})
    if result.deleted_count == 0:
        raise HTTPException(404, "Member not found")
    await db.conversations.update_many({"tenant_id": admin["tenant_id"], "assigned_to": user_id}, {"$set": {"assigned_to": None}})
    return {"status": "removed"}


# ---------- Tenant settings ----------

@api_router.get("/tenant")
async def get_tenant(user=Depends(get_current_user)):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    if user["role"] != "admin":
        token = tenant.get("marketly_bearer_token") or ""
        tenant["marketly_bearer_token"] = token[:4] + "•" * 12 if token else ""
    return tenant


@api_router.post("/tenant/regenerate-key")
async def regenerate_key(admin=Depends(require_admin)):
    new_key = "sk_" + secrets.token_hex(20)
    await db.tenants.update_one({"id": admin["tenant_id"]}, {"$set": {"api_key": new_key}})
    return {"api_key": new_key}


@api_router.patch("/tenant")
async def update_tenant(data: TenantPatch, admin=Depends(require_admin)):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if "evolution_instance_name" in updates:
        updates["evolution_instance_name"] = updates["evolution_instance_name"].strip()
    if updates:
        await db.tenants.update_one({"id": admin["tenant_id"]}, {"$set": updates})
    if updates.get("evolution_instance_name"):
        try:
            await evolution_request("POST", "/instance/create", {
                "instanceName": updates["evolution_instance_name"],
                "integration": "WHATSAPP-BAILEYS", "qrcode": False,
            })
        except HTTPException:
            pass  # instance may already exist; connect via QR from Settings
    return await db.tenants.find_one({"id": admin["tenant_id"]}, {"_id": 0})


@api_router.get("/tenant/evolution/qrcode")
async def evolution_qrcode(user=Depends(get_current_user)):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]})
    instance = (tenant or {}).get("evolution_instance_name")
    if not instance:
        raise HTTPException(400, "Set the Evolution instance name first")
    state_resp = await evolution_request("GET", f"/instance/connectionState/{instance}")
    state = (state_resp.get("instance") or {}).get("state") or state_resp.get("state")
    if state == "open":
        return {"state": "open", "base64": None}
    resp = await evolution_request("GET", f"/instance/connect/{instance}")
    b64 = resp.get("base64") or (resp.get("qrcode") or {}).get("base64")
    if b64 and not b64.startswith("data:"):
        b64 = f"data:image/png;base64,{b64}"
    return {"state": state, "base64": b64}


@api_router.post("/tenant/webhook/configure")
async def configure_webhook(request: Request, admin=Depends(require_admin)):
    tenant = await db.tenants.find_one({"id": admin["tenant_id"]})
    instance = (tenant or {}).get("evolution_instance_name")
    if not instance:
        raise HTTPException(400, "Set the Evolution instance name first")
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    url = f"{proto}://{host}/api/webhook/inbound/{tenant['id']}"
    resp = await evolution_request("POST", f"/webhook/set/{instance}", {"webhook": {
        "enabled": True, "url": url, "webhookByEvents": False,
        "webhookBase64": True, "events": ["MESSAGES_UPSERT"],
    }})
    return {"status": "configured", "webhook_url": url, "evolution": resp}


@api_router.get("/tenant/evolution/status")
async def evolution_status(user=Depends(get_current_user)):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]})
    instance = (tenant or {}).get("evolution_instance_name")
    if not instance:
        return {"configured": False, "state": None}
    try:
        resp = await evolution_request("GET", f"/instance/connectionState/{instance}")
        return {"configured": True, "state": (resp.get("instance") or {}).get("state") or resp.get("state")}
    except HTTPException as e:
        return {"configured": True, "state": None, "error": e.detail}


@api_router.get("/")
async def root():
    return {"message": "Slash API"}


app.include_router(api_router)
app.mount("/api/files", StaticFiles(directory=str(UPLOAD_DIR)), name="files")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.contacts.create_index([("tenant_id", 1), ("phone", 1)])
    await db.conversations.create_index([("tenant_id", 1), ("status", 1)])
    await db.messages.create_index([("tenant_id", 1), ("external_id", 1)])
    await db.tenants.update_many({"evolution_instance_name": {"$exists": False}}, {"$set": {"evolution_instance_name": ""}})
    admin_email = os.environ["ADMIN_EMAIL"].lower()
    admin_password = os.environ["ADMIN_PASSWORD"]
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        tenant = {
            "id": str(uuid.uuid4()),
            "company_name": "Marketly Tech",
            "evolution_instance_name": os.environ.get("EVOLUTION_INSTANCE_NAME", ""),
            "marketly_instance_id": os.environ.get("WHATSAPP_INSTANCE_ID", ""),
            "marketly_bearer_token": os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
            "api_key": "sk_" + secrets.token_hex(20),
            "created_at": utcnow(),
        }
        await db.tenants.insert_one(tenant)
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "tenant_id": tenant["id"],
            "name": "Marketly Admin",
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "role": "admin",
            "created_at": utcnow(),
        })
        logger.info("Seeded default tenant and admin user")
    elif not verify_password(admin_password, existing["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_password)}})


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
