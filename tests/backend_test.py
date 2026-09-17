"""Backend tests for Slash multi-tenant WhatsApp inbox."""
import os
import time
import uuid
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://conversation-dash.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "marketlytech@gmail.com"
ADMIN_PASSWORD = "#Slash123"


# ---------- Fixtures ----------

@pytest.fixture(scope="session")
def admin_ctx():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"token": d["token"], "tenant_id": d["tenant"]["id"], "user_id": d["user"]["id"], "tenant": d["tenant"]}


@pytest.fixture(scope="session")
def admin_headers(admin_ctx):
    return {"Authorization": f"Bearer {admin_ctx['token']}"}


@pytest.fixture(scope="session")
def second_tenant():
    rnd = uuid.uuid4().hex[:8]
    payload = {
        "company_name": f"TEST_Tenant_{rnd}",
        "admin_name": "TEST Admin",
        "email": f"test-tenant-{rnd}@example.com",
        "password": "TestPass123",
        "marketly_instance_id": "TEST_INST",
        "marketly_bearer_token": "TEST_TOKEN",
    }
    r = requests.post(f"{API}/auth/signup", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()
    return {"token": d["token"], "tenant_id": d["tenant"]["id"], "email": payload["email"], "password": payload["password"]}


# ---------- Auth ----------

class TestAuth:
    def test_login_seeded_admin(self, admin_ctx):
        assert admin_ctx["token"]
        assert admin_ctx["tenant"]["company_name"] == "Marketly Tech"

    def test_login_bad_password(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert r.status_code == 401

    def test_me(self, admin_headers):
        r = requests.get(f"{API}/auth/me", headers=admin_headers)
        assert r.status_code == 200
        d = r.json()
        assert d["user"]["email"] == ADMIN_EMAIL
        assert d["tenant"]["id"]
        # No mongo _id leak
        assert "_id" not in d["user"] and "_id" not in d["tenant"]

    def test_signup_duplicate_email(self, second_tenant):
        r = requests.post(f"{API}/auth/signup", json={
            "company_name": "Dup", "admin_name": "Dup", "email": second_tenant["email"],
            "password": "TestPass123", "marketly_instance_id": "x", "marketly_bearer_token": "y",
        })
        assert r.status_code == 409

    def test_protected_requires_auth(self):
        r = requests.get(f"{API}/conversations")
        assert r.status_code == 401


# ---------- Webhook ----------

class TestWebhook:
    def test_flat_text_ingest(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        payload = {"phone": phone, "name": "TEST Flat", "type": "text", "text": "hello flat",
                   "message_id": "TESTMSG_" + uuid.uuid4().hex}
        r = requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}", json=payload)
        assert r.status_code == 200
        assert r.json()["received"] == 1
        # Find conversation
        time.sleep(0.3)
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        assert r.status_code == 200
        convs = r.json()
        assert any(c["contact_phone"] == phone for c in convs)
        conv = next(c for c in convs if c["contact_phone"] == phone)
        assert conv["unread_count"] >= 1
        assert "hello flat" in (conv.get("last_message_preview") or "")

    def test_dedupe_external_id(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        mid = "DEDUP_" + uuid.uuid4().hex
        p = {"phone": phone, "type": "text", "text": "dup", "message_id": mid}
        r1 = requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}", json=p)
        r2 = requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}", json=p)
        assert r1.json()["received"] == 1
        assert r2.json()["received"] == 0

    def test_cloud_api_nested(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        mid = "CLOUD_" + uuid.uuid4().hex
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "contacts": [{"wa_id": phone, "profile": {"name": "TEST Cloud"}}],
                        "messages": [{
                            "from": phone, "id": mid, "type": "text",
                            "text": {"body": "cloud hello"},
                        }],
                    }
                }]
            }]
        }
        r = requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}", json=payload)
        assert r.status_code == 200
        assert r.json()["received"] == 1

    def test_unknown_tenant(self):
        r = requests.post(f"{API}/webhook/inbound/does-not-exist", json={"phone": "1", "text": "x"})
        assert r.status_code == 404

    @pytest.mark.parametrize("mtype,extra", [
        ("image", {"media_url": "https://picsum.photos/200"}),
        ("document", {"media_url": "https://www.w3.org/dummy.pdf", "filename": "dummy.pdf"}),
        ("audio", {"media_url": "https://example.com/a.mp3"}),
        ("video", {"media_url": "https://example.com/v.mp4"}),
    ])
    def test_media_types(self, admin_ctx, mtype, extra):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        payload = {"phone": phone, "type": mtype, "message_id": f"{mtype}_{uuid.uuid4().hex}", **extra}
        r = requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}", json=payload)
        assert r.status_code == 200
        assert r.json()["received"] == 1


# ---------- Conversation flows ----------

class TestConversations:
    def _make_conv(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}",
                      json={"phone": phone, "type": "text", "text": "seed",
                            "message_id": "S_" + uuid.uuid4().hex})
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        return next(c for c in r.json() if c["contact_phone"] == phone)

    def test_open_marks_read(self, admin_ctx, admin_headers):
        conv = self._make_conv(admin_ctx, admin_headers)
        assert conv["unread_count"] >= 1
        r = requests.get(f"{API}/conversations/{conv['id']}/messages", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list) and len(r.json()) >= 1
        r2 = requests.get(f"{API}/conversations/{conv['id']}", headers=admin_headers)
        assert r2.json()["unread_count"] == 0

    def test_status_change(self, admin_ctx, admin_headers):
        conv = self._make_conv(admin_ctx, admin_headers)
        r = requests.patch(f"{API}/conversations/{conv['id']}", headers=admin_headers,
                          json={"status": "resolved"})
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"
        # invalid status
        r = requests.patch(f"{API}/conversations/{conv['id']}", headers=admin_headers,
                          json={"status": "bogus"})
        assert r.status_code == 400

    def test_add_internal_note(self, admin_ctx, admin_headers):
        conv = self._make_conv(admin_ctx, admin_headers)
        # get preview before
        r0 = requests.get(f"{API}/conversations/{conv['id']}", headers=admin_headers)
        prev_preview = r0.json().get("last_message_preview")
        r = requests.post(f"{API}/conversations/{conv['id']}/notes", headers=admin_headers,
                         json={"body": "internal note test"})
        assert r.status_code == 200
        n = r.json()
        assert n["is_note"] is True and n["type"] == "note"
        # preview unchanged
        r2 = requests.get(f"{API}/conversations/{conv['id']}", headers=admin_headers)
        assert r2.json().get("last_message_preview") == prev_preview

    def test_assign_conversation(self, admin_ctx, admin_headers):
        conv = self._make_conv(admin_ctx, admin_headers)
        r = requests.patch(f"{API}/conversations/{conv['id']}", headers=admin_headers,
                          json={"assigned_to": admin_ctx["user_id"]})
        assert r.status_code == 200
        assert r.json()["assigned_to"] == admin_ctx["user_id"]
        # bogus assignee
        r = requests.patch(f"{API}/conversations/{conv['id']}", headers=admin_headers,
                          json={"assigned_to": "not-a-user"})
        assert r.status_code == 400


# ---------- Contacts ----------

class TestContacts:
    def test_list_and_update_contact(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}",
                      json={"phone": phone, "type": "text", "text": "hi",
                            "message_id": "C_" + uuid.uuid4().hex})
        r = requests.get(f"{API}/contacts", headers=admin_headers, params={"search": phone})
        assert r.status_code == 200
        contact = next(c for c in r.json() if c["phone"] == phone)
        assert "conversation_count" in contact
        # update
        r = requests.patch(f"{API}/contacts/{contact['id']}", headers=admin_headers,
                          json={"name": "TEST Updated", "labels": ["vip", "test"], "notes": "some notes"})
        assert r.status_code == 200
        c2 = r.json()
        assert c2["name"] == "TEST Updated"
        assert set(c2["labels"]) == {"vip", "test"}


# ---------- Team ----------

class TestTeam:
    def test_invite_and_agent_403(self, admin_ctx, admin_headers):
        agent_email = f"test-agent-{uuid.uuid4().hex[:8]}@example.com"
        agent_pw = "AgentPass123"
        r = requests.post(f"{API}/team/invite", headers=admin_headers,
                        json={"name": "TEST Agent", "email": agent_email, "password": agent_pw, "role": "agent"})
        assert r.status_code == 200
        # agent login
        r = requests.post(f"{API}/auth/login", json={"email": agent_email, "password": agent_pw})
        assert r.status_code == 200
        agent_token = r.json()["token"]
        # agent cannot invite
        r = requests.post(f"{API}/team/invite",
                        headers={"Authorization": f"Bearer {agent_token}"},
                        json={"name": "X", "email": f"x-{uuid.uuid4().hex[:6]}@e.com", "password": "abcdef1", "role": "agent"})
        assert r.status_code == 403


# ---------- Tenant settings & isolation ----------

class TestTenantSettings:
    def test_tenant_settings(self, admin_headers):
        r = requests.get(f"{API}/tenant", headers=admin_headers)
        assert r.status_code == 200
        t = r.json()
        assert t["api_key"].startswith("sk_")
        assert "marketly_instance_id" in t

    def test_regenerate_key(self, admin_headers):
        r = requests.get(f"{API}/tenant", headers=admin_headers)
        old = r.json()["api_key"]
        r = requests.post(f"{API}/tenant/regenerate-key", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["api_key"].startswith("sk_") and r.json()["api_key"] != old


class TestIsolation:
    def test_second_tenant_isolated(self, admin_ctx, admin_headers, second_tenant):
        # create a conversation in tenant 1
        phone = "9198" + str(uuid.uuid4().int)[:10]
        requests.post(f"{API}/webhook/inbound/{admin_ctx['tenant_id']}",
                      json={"phone": phone, "type": "text", "text": "t1 only",
                            "message_id": "ISO_" + uuid.uuid4().hex})
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv_id = next(c for c in r.json() if c["contact_phone"] == phone)["id"]

        # tenant 2 should not see it
        h2 = {"Authorization": f"Bearer {second_tenant['token']}"}
        r = requests.get(f"{API}/conversations", headers=h2)
        assert r.status_code == 200
        assert all(c["id"] != conv_id for c in r.json())
        # cross-tenant fetch = 404
        r = requests.get(f"{API}/conversations/{conv_id}", headers=h2)
        assert r.status_code == 404
        r = requests.get(f"{API}/conversations/{conv_id}/messages", headers=h2)
        assert r.status_code == 404
