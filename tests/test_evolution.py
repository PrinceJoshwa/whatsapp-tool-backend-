"""Session 2: Evolution API integration backend tests."""
import base64
import os
import time
import uuid
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://conversation-dash.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "marketlytech@gmail.com"
ADMIN_PASSWORD = "#Slash123"

# 1x1 transparent PNG
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


@pytest.fixture(scope="module")
def admin_ctx():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"token": d["token"], "tenant_id": d["tenant"]["id"], "user_id": d["user"]["id"], "tenant": d["tenant"]}


@pytest.fixture(scope="module")
def admin_headers(admin_ctx):
    return {"Authorization": f"Bearer {admin_ctx['token']}"}


@pytest.fixture(scope="module")
def agent_headers(admin_headers):
    email = f"test-agent-evo-{uuid.uuid4().hex[:8]}@example.com"
    pw = "AgentPass123"
    r = requests.post(f"{API}/team/invite", headers=admin_headers,
                      json={"name": "TEST Evo Agent", "email": email, "password": pw, "role": "agent"})
    assert r.status_code == 200, r.text
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _evo_upsert(tenant_id, phone, external_id, message=None, from_me=False, push_name="Evo Cust"):
    payload = {
        "event": "messages.upsert",
        "instance": "demo-instance",
        "data": {
            "key": {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": from_me, "id": external_id},
            "pushName": push_name,
            "message": message or {"conversation": "hello evo"},
            "messageTimestamp": int(time.time()),
        },
    }
    return requests.post(f"{API}/webhook/inbound/{tenant_id}", json=payload)


# ---------- Evolution webhook parsing ----------

class TestEvolutionWebhook:
    def test_text_conversation(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        r = _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_TXT_{uuid.uuid4().hex}")
        assert r.status_code == 200
        assert r.json()["received"] == 1
        time.sleep(0.3)
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv = next(c for c in r.json() if c["contact_phone"] == phone)
        assert conv["unread_count"] >= 1
        assert "hello evo" in (conv.get("last_message_preview") or "")

    def test_text_extended_message(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        msg = {"extendedTextMessage": {"text": "extended hello"}}
        r = _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_EXT_{uuid.uuid4().hex}", message=msg)
        assert r.status_code == 200 and r.json()["received"] == 1
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv = next(c for c in r.json() if c["contact_phone"] == phone)
        assert "extended hello" in (conv.get("last_message_preview") or "")

    def test_from_me_outbound_no_unread(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        # First an inbound to create conversation
        _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_IN_{uuid.uuid4().hex}")
        time.sleep(0.2)
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv_before = next(c for c in r.json() if c["contact_phone"] == phone)
        unread_before = conv_before["unread_count"]
        # Now a fromMe echo
        r = _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_OUT_{uuid.uuid4().hex}",
                        message={"conversation": "echo from phone"}, from_me=True)
        assert r.status_code == 200 and r.json()["received"] == 1
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv_after = next(c for c in r.json() if c["contact_phone"] == phone)
        # unread should NOT increase for fromMe
        assert conv_after["unread_count"] == unread_before
        assert (conv_after.get("last_message_preview") or "").startswith("You:")
        # verify message direction
        msgs = requests.get(f"{API}/conversations/{conv_after['id']}/messages", headers=admin_headers).json()
        assert any(m.get("direction") == "outbound" and m.get("text") == "echo from phone" for m in msgs)

    def test_dedupe_evolution_id(self, admin_ctx):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        mid = f"EVO_DUP_{uuid.uuid4().hex}"
        r1 = _evo_upsert(admin_ctx["tenant_id"], phone, mid)
        r2 = _evo_upsert(admin_ctx["tenant_id"], phone, mid)
        assert r1.json()["received"] == 1
        assert r2.json()["received"] == 0

    @pytest.mark.parametrize("mkey,mtype", [
        ("imageMessage", "image"),
        ("videoMessage", "video"),
        ("audioMessage", "audio"),
        ("documentMessage", "document"),
    ])
    def test_media_types(self, admin_ctx, mkey, mtype):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        msg = {mkey: {"url": f"https://example.com/x.{mtype}", "caption": f"cap {mtype}",
                      "mimetype": f"{mtype}/x", "fileName": f"f.{mtype}"}}
        r = _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_{mtype}_{uuid.uuid4().hex}", message=msg)
        assert r.status_code == 200 and r.json()["received"] == 1

    def test_document_with_caption_nested(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        msg = {"documentWithCaptionMessage": {
            "documentMessage": {
                "url": "https://example.com/doc.pdf", "caption": "nested cap",
                "mimetype": "application/pdf", "fileName": "doc.pdf",
            }}}
        r = _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_DWC_{uuid.uuid4().hex}", message=msg)
        assert r.status_code == 200 and r.json()["received"] == 1
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv = next(c for c in r.json() if c["contact_phone"] == phone)
        msgs = requests.get(f"{API}/conversations/{conv['id']}/messages", headers=admin_headers).json()
        m = msgs[-1]
        assert m["type"] == "document"
        assert m.get("filename") == "doc.pdf"
        assert m.get("caption") == "nested cap"

    def test_base64_media_saved_and_served(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        msg = {"imageMessage": {"caption": "b64 img", "mimetype": "image/png", "fileName": "tiny.png"},
               "base64": PNG_B64}
        r = _evo_upsert(admin_ctx["tenant_id"], phone, f"EVO_B64_{uuid.uuid4().hex}", message=msg)
        assert r.status_code == 200 and r.json()["received"] == 1
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        conv = next(c for c in r.json() if c["contact_phone"] == phone)
        msgs = requests.get(f"{API}/conversations/{conv['id']}/messages", headers=admin_headers).json()
        img_msg = next(m for m in msgs if m["type"] == "image")
        media_url = img_msg.get("media_url") or ""
        assert media_url.startswith("/api/files/"), f"expected /api/files/... got {media_url}"
        # Fetch the served file
        full = f"{BASE_URL}{media_url}"
        r = requests.get(full)
        assert r.status_code == 200
        assert r.content == base64.b64decode(PNG_B64)


# ---------- Outbound send endpoints ----------

class TestOutboundNoInstance:
    """Ensure endpoints 400 when instance not configured."""

    @pytest.fixture(scope="class")
    def temp_admin(self):
        """Create a fresh tenant with NO evolution instance."""
        rnd = uuid.uuid4().hex[:8]
        payload = {
            "company_name": f"TEST_EvoNone_{rnd}", "admin_name": "TEST Admin",
            "email": f"test-evo-none-{rnd}@example.com", "password": "TestPass123",
            "evolution_instance_name": "",
        }
        r = requests.post(f"{API}/auth/signup", json=payload)
        assert r.status_code == 200
        d = r.json()
        return {"token": d["token"], "tenant_id": d["tenant"]["id"]}

    def _conv(self, ctx):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        _evo_upsert(ctx["tenant_id"], phone, f"NO_INST_{uuid.uuid4().hex}")
        h = {"Authorization": f"Bearer {ctx['token']}"}
        r = requests.get(f"{API}/conversations", headers=h, params={"search": phone})
        return next(c for c in r.json() if c["contact_phone"] == phone), h

    def test_send_text_no_instance(self, temp_admin):
        conv, h = self._conv(temp_admin)
        r = requests.post(f"{API}/conversations/{conv['id']}/send", headers=h, json={"text": "hi"})
        assert r.status_code == 400
        assert "instance" in (r.json().get("detail") or "").lower()

    def test_send_link_no_instance(self, temp_admin):
        conv, h = self._conv(temp_admin)
        r = requests.post(f"{API}/conversations/{conv['id']}/send-link", headers=h,
                          json={"url": "https://example.com/a.pdf", "caption": ""})
        assert r.status_code == 400
        assert "instance" in (r.json().get("detail") or "").lower()

    def test_send_file_no_instance(self, temp_admin):
        conv, h = self._conv(temp_admin)
        files = {"file": ("t.txt", b"hello", "text/plain")}
        r = requests.post(f"{API}/conversations/{conv['id']}/send-file", headers=h,
                          data={"caption": ""}, files=files)
        assert r.status_code == 400


class TestOutboundWithInstance:
    """Send endpoints with instance configured should reach Evolution and
    return clean JSON 400 (instance not connected) NOT HTML 502."""

    def _conv(self, admin_ctx, admin_headers):
        phone = "9198" + str(uuid.uuid4().int)[:10]
        _evo_upsert(admin_ctx["tenant_id"], phone, f"SEND_{uuid.uuid4().hex}")
        r = requests.get(f"{API}/conversations", headers=admin_headers, params={"search": phone})
        return next(c for c in r.json() if c["contact_phone"] == phone)

    def _msg_count(self, admin_headers, cid):
        return len(requests.get(f"{API}/conversations/{cid}/messages", headers=admin_headers).json())

    def test_send_text_returns_json_error(self, admin_ctx, admin_headers):
        # ensure instance is set to demo-instance
        r = requests.patch(f"{API}/tenant", headers=admin_headers,
                           json={"evolution_instance_name": "demo-instance"})
        assert r.status_code == 200
        conv = self._conv(admin_ctx, admin_headers)
        before = self._msg_count(admin_headers, conv["id"])
        t0 = time.time()
        r = requests.post(f"{API}/conversations/{conv['id']}/send", headers=admin_headers,
                          json={"text": "test message"}, timeout=60)
        elapsed = time.time() - t0
        # Should be clean JSON — not HTML
        assert r.headers.get("content-type", "").startswith("application/json"), \
            f"expected JSON, got {r.headers.get('content-type')}: {r.text[:200]}"
        # Either it succeeded (unlikely, no real phone connected) or clean 400
        assert r.status_code in (200, 400)
        assert elapsed < 35, f"took {elapsed}s, should be under 35s"
        # On failure, message NOT recorded
        if r.status_code == 400:
            after = self._msg_count(admin_headers, conv["id"])
            assert after == before, "outbound message should NOT be recorded when Evolution fails"

    def test_send_link_returns_json_error(self, admin_ctx, admin_headers):
        conv = self._conv(admin_ctx, admin_headers)
        before = self._msg_count(admin_headers, conv["id"])
        t0 = time.time()
        r = requests.post(f"{API}/conversations/{conv['id']}/send-link", headers=admin_headers,
                          json={"url": "https://example.com/a.pdf", "caption": "hi"}, timeout=60)
        elapsed = time.time() - t0
        assert r.headers.get("content-type", "").startswith("application/json"), r.text[:200]
        assert r.status_code in (200, 400)
        assert elapsed < 35
        if r.status_code == 400:
            assert self._msg_count(admin_headers, conv["id"]) == before

    def test_send_file_returns_json_error(self, admin_ctx, admin_headers):
        conv = self._conv(admin_ctx, admin_headers)
        before = self._msg_count(admin_headers, conv["id"])
        files = {"file": ("tiny.png", base64.b64decode(PNG_B64), "image/png")}
        t0 = time.time()
        r = requests.post(f"{API}/conversations/{conv['id']}/send-file", headers=admin_headers,
                          data={"caption": "img"}, files=files, timeout=60)
        elapsed = time.time() - t0
        assert r.headers.get("content-type", "").startswith("application/json"), r.text[:200]
        assert r.status_code in (200, 400)
        assert elapsed < 35
        if r.status_code == 400:
            assert self._msg_count(admin_headers, conv["id"]) == before


# ---------- Tenant / Evolution admin endpoints ----------

class TestTenantEvolution:
    def test_patch_tenant_admin_only(self, admin_headers, agent_headers):
        # agent cannot patch
        r = requests.patch(f"{API}/tenant", headers=agent_headers,
                           json={"evolution_instance_name": "hackattempt"})
        assert r.status_code == 403
        # admin can patch
        r = requests.patch(f"{API}/tenant", headers=admin_headers,
                           json={"evolution_instance_name": "demo-instance"})
        assert r.status_code == 200
        assert r.json()["evolution_instance_name"] == "demo-instance"

    def test_evolution_status(self, admin_headers):
        # ensure instance set
        requests.patch(f"{API}/tenant", headers=admin_headers,
                       json={"evolution_instance_name": "demo-instance"})
        r = requests.get(f"{API}/tenant/evolution/status", headers=admin_headers, timeout=60)
        assert r.headers.get("content-type", "").startswith("application/json"), r.text[:200]
        assert r.status_code == 200
        d = r.json()
        assert d["configured"] is True
        # state can be None if evolution unreachable, or a string
        assert "state" in d

    def test_webhook_configure(self, admin_headers):
        requests.patch(f"{API}/tenant", headers=admin_headers,
                       json={"evolution_instance_name": "demo-instance"})
        r = requests.post(f"{API}/tenant/webhook/configure", headers=admin_headers, timeout=60)
        assert r.headers.get("content-type", "").startswith("application/json"), r.text[:200]
        # Success 200 or clean 400 from Evolution — must not be HTML
        assert r.status_code in (200, 400)
        if r.status_code == 200:
            d = r.json()
            assert d.get("status") == "configured"
            assert "/api/webhook/inbound/" in d.get("webhook_url", "")


# ---------- Signup with instance name ----------

class TestSignupEvolution:
    def test_signup_with_instance_name(self):
        rnd = uuid.uuid4().hex[:8]
        payload = {
            "company_name": f"TEST_EvoSignup_{rnd}", "admin_name": "TEST Admin",
            "email": f"test-evo-signup-{rnd}@example.com", "password": "TestPass123",
            "evolution_instance_name": f"tenant-{rnd}",
        }
        r = requests.post(f"{API}/auth/signup", json=payload)
        assert r.status_code == 200
        d = r.json()
        assert d["tenant"]["evolution_instance_name"] == f"tenant-{rnd}"
