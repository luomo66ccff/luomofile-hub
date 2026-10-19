"""Exercise format delivery, identity isolation, HEAD and revocation in a temporary DB."""
import base64
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = tempfile.TemporaryDirectory(prefix="luomofile-format-smoke-", ignore_cleanup_errors=True)
root = Path(temporary.name)
for key, value in {
    "DATABASE_PATH": str(root / "test.db"), "LOCAL_TEMP_PATH": str(root / "storage"),
    "APP_BASE_URL": "https://file.example.test", "APP_ENV": "test",
    "SESSION_SECRET": "test-session-secret-with-more-than-32-bytes",
    "SUBSCRIPTION_TOKEN_ENCRYPTION_SECRET": "test-token-encryption-secret-more-than-32-bytes",
    "SUBSCRIPTION_IDENTITY_SECRET": "test-identity-secret-more-than-32-bytes",
    "SUBSCRIPTION_IDENTITY_SYNC_ENABLED": "false", "COS_ENABLED": "false", "LOCAL_TEMP_ENABLED": "true",
}.items():
    os.environ[key] = value

import yaml
from fastapi.testclient import TestClient
from app.db import connect, init_db
import app.main as main_module
from app.security import SESSION_COOKIE, hash_token, now_iso, sign_session
from app.subscriptions import create_subscription_link, revoke_subscription_link
import app.subscriptions as subscriptions
from app.subscription_identity import derive_subscription_identity, render_subscription_payload
from app.subscription_formats import ConversionError, FORMAT_OPTIONS, convert


def template():
    proxies = [
        {"name": "Public-测试", "type": "vmess", "server": "1.1.1.1", "port": 443, "uuid": "legacy-uuid", "alterId": 0, "cipher": "auto", "tls": True, "servername": "tls.example.test", "network": "ws", "ws-opts": {"path": "/ws?ed=0", "headers": {"Host": "ws.example.test"}}},
        {"name": "Other", "type": "vmess", "server": "proxy.example.test", "port": 443, "uuid": "legacy-uuid", "alterId": 0, "cipher": "auto", "tls": True, "servername": "proxy.example.test"},
        {"name": "Private-HY2", "type": "hysteria2", "server": "10.5.11.1", "port": 8443, "password": "legacy-hy2"},
        {"name": "Private-TUIC", "type": "tuic", "server": "10.5.11.1", "port": 8444, "uuid": "legacy-tuic", "password": "legacy-tuic-pass"},
        {"name": "Public-SS", "type": "ss", "server": "8.8.8.8", "port": 8388, "cipher": "aes-256-gcm", "password": "legacy-ss"},
        {"name": "Private-SS", "type": "ss", "server": "10.5.11.1", "port": 8388, "cipher": "aes-256-gcm", "password": "legacy-ss"},
    ]
    groups = [
        {"name": "AUTO", "type": "url-test", "proxies": ["Public-测试", "Other", "Public-SS"], "url": "https://cp.cloudflare.com/generate_204", "interval": 120, "timeout": 5000, "max-failed-times": 1},
        {"name": "ZEROTIER", "type": "select", "proxies": ["Private-HY2", "Private-TUIC", "Private-SS", "DIRECT"]},
        {"name": "PROXY", "type": "select", "proxies": ["AUTO", "ZEROTIER", "DIRECT"]},
    ]
    # The production identity renderer deliberately accepts its existing block layout.
    text = "proxies:\n"
    for proxy in proxies:
        for index, (key, value) in enumerate(proxy.items()):
            scalar = value if key == "type" else json.dumps(value, ensure_ascii=False)
            text += ("- " if index == 0 else "  ") + key + ": " + scalar + "\n"
    text += yaml.safe_dump({"proxy-groups": groups, "rules": ["GEOIP,CN,DIRECT", "MATCH,PROXY"]}, sort_keys=False, allow_unicode=True)
    return text.encode()


def use_count(token_id):
    with connect() as conn:
        return conn.execute("SELECT use_count FROM subscription_tokens WHERE id=?", (token_id,)).fetchone()[0]


def credentials(response, fmt):
    if fmt in {"mihomo", "clash"}:
        nodes = yaml.safe_load(response.content)["proxies"]
        return [str(p[key]) for p in nodes for key in ("uuid", "password") if key in p]
    if fmt == "sing-box":
        nodes = response.json()["outbounds"]
        return [str(p[key]) for p in nodes for key in ("uuid", "password") if key in p]
    text = base64.b64decode(response.content).decode() if fmt == "base64" else response.text
    result = []
    for line in text.splitlines():
        if line.startswith("vmess://"):
            result.append(json.loads(base64.b64decode(line[8:]))["id"])
        elif line.startswith("ss://"):
            encoded = line[5:].split("@", 1)[0]
            result.append(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode().split(":", 1)[1])
        else:
            raise AssertionError("Unexpected URI scheme")
    return result


class SubscriptionControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pickers = []
        self.url_inputs = []
        self.current_picker = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select" and "data-subscription-format" in attrs:
            self.current_picker = []
            self.pickers.append(self.current_picker)
        elif tag == "option" and self.current_picker is not None:
            self.current_picker.append(attrs)
        elif tag == "input" and "data-subscription-url" in attrs:
            self.url_inputs.append(attrs["value"])

    def handle_endtag(self, tag):
        if tag == "select":
            self.current_picker = None


def main():
    init_db()
    payload = template()
    storage = root / "storage/other/subscription.yml"
    storage.parent.mkdir(parents=True)
    storage.write_bytes(payload)
    with connect() as conn:
        owner = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()[0]
        conn.execute("""INSERT INTO files(file_id,owner_id,original_filename,stored_filename,extension,mime_type,size_bytes,sha256,storage_backend,bucket_name,object_key,link_enabled,visibility,purpose,status,uploaded_at,updated_at)
            VALUES ('lf_formats',?,'subscription.yml','subscription.yml','.yml','text/yaml',?,?,'local_temp','local','other/subscription.yml',0,'private','other','active',?,?)""", (owner,len(payload),hashlib.sha256(payload).hexdigest(),now_iso(),now_iso()))
        conn.commit()
    grants = [create_subscription_link("lf_formats", label, owner, 1) for label in ("format-a", "format-b")]
    tokens = [grant["url"].rsplit("/", 1)[1] for grant in grants]
    identities = [derive_subscription_identity(hash_token(token)) for token in tokens]
    formats = list(FORMAT_OPTIONS)
    checks = 0
    with TestClient(main_module.app, follow_redirects=False) as client:
        baseline = render_subscription_payload(payload, identities[0])
        for agent in ("clash/2023.08.17", "mihomo/1.19.28", "v2rayN/7.0", "Shadowrocket"):
            assert client.get(f"/sub/{tokens[0]}", headers={"User-Agent": agent}).content == baseline
            checks += 1
        for fmt in formats:
            for index, token in enumerate(tokens):
                url = f"/sub/{token}?format={fmt}"
                response = client.get(url)
                assert response.status_code == 200
                assert response.headers["x-subscription-format"] == fmt
                assert response.headers["content-type"].startswith(FORMAT_OPTIONS[fmt]["media_type"].split(";")[0])
                assert response.headers["cache-control"] == "private, no-store, max-age=0"
                assert response.headers["x-content-type-options"] == "nosniff"
                current = use_count(grants[index]["id"])
                head = client.head(url)
                assert head.status_code == 200 and head.content == b""
                assert int(head.headers["content-length"]) == len(response.content)
                assert head.headers["content-type"] == response.headers["content-type"]
                assert use_count(grants[index]["id"]) == current
                creds = credentials(response, fmt)
                assert identities[index].vmess_uuid in creds
                assert identities[index].shadowsocks_password in creds
                assert identities[1-index].vmess_uuid not in creds
                assert identities[1-index].shadowsocks_password not in creds
                assert "legacy-uuid" not in creds and "legacy-ss" not in creds
                if fmt == "mihomo":
                    assert response.content == render_subscription_payload(payload, identities[index])
                else:
                    assert len(creds) == 3 and "10.5.11.1" not in response.text
                checks += 1
        for fmt in formats[1:]:
            response = client.get(f"/sub/{tokens[0]}?format={fmt}&include_private=1")
            assert response.status_code == 200 and len(credentials(response, fmt)) == 4
            checks += 1
        for query in ("format=unknown", "format=", "format=clash&format=uri", "format=clash&include_private=yes", "include_private=0&include_private=1"):
            before = use_count(grants[0]["id"])
            assert client.get(f"/sub/{tokens[0]}?{query}").status_code == 400
            assert use_count(grants[0]["id"]) == before
            assert client.get(f"/sub/lsub_invalid?{query}").status_code == 404
            checks += 1
        # Public reads must not synchronously wait for node reconciliation.
        real_sync = subscriptions.sync_identity_records
        sync_calls = []
        def blocked(_records):
            sync_calls.append(True)
            raise RuntimeError("controlled unavailable node")
        subscriptions.sync_identity_records = blocked
        try:
            before = use_count(grants[0]["id"])
            for fmt in formats:
                response = client.get(f"/sub/{tokens[0]}?format={fmt}")
                head = client.head(f"/sub/{tokens[0]}?format={fmt}")
                assert response.status_code == 200 and head.status_code == 200
                assert identities[0].vmess_uuid in credentials(response, fmt)
                checks += 1
            assert use_count(grants[0]["id"]) == before + len(formats)
            assert not sync_calls
        finally:
            subscriptions.sync_identity_records = real_sync
        # Refuse a profile with no publicly usable compatible nodes.
        private_payload = payload.replace(b'"1.1.1.1"',b'"10.0.0.1"').replace(b'"proxy.example.test"',b'"10.0.0.1"').replace(b'"8.8.8.8"',b'"10.0.0.1"')
        storage.write_bytes(private_payload)
        for fmt in formats[1:]:
            assert client.get(f"/sub/{tokens[0]}?format={fmt}").status_code == 503
            checks += 1
        storage.write_bytes(payload)
        # Verify all copy URLs are admin-only and disappear after revocation.
        assert client.get("/admin/subscriptions").status_code == 401
        client.cookies.set(SESSION_COOKIE, sign_session("luomo"))
        page = client.get("/admin/subscriptions")
        assert page.status_code == 200
        for fmt in formats:
            selected_page = client.get(f"/admin/subscriptions?format={fmt}")
            assert selected_page.status_code == 200
            controls = SubscriptionControls()
            controls.feed(selected_page.text)
            assert len(controls.pickers) == len(grants)
            for options in controls.pickers:
                assert len(options) == len(formats)
                assert {option["value"] for option in options} == set(formats)
                assert [option["value"] for option in options if "selected" in option] == [fmt]
            expected_urls = {grant["url"] + (f"?format={fmt}" if fmt != "mihomo" else "") for grant in grants}
            assert len(controls.url_inputs) == len(grants)
            assert set(controls.url_inputs) == expected_urls
            checks += 1
        export = os.environ.get("FORMAT_QA_HTML")
        if export:
            Path(export).parent.mkdir(parents=True, exist_ok=True)
            Path(export).write_text(page.text, encoding="utf-8")
        client.cookies.clear()
        assert revoke_subscription_link(grants[0]["id"]) == "revoked"
        for fmt in formats:
            response = client.get(f"/sub/{tokens[0]}?format={fmt}")
            assert response.status_code == 404 and "location" not in response.headers
            assert client.head(f"/sub/{tokens[0]}?format={fmt}").status_code == 404
            checks += 1
        client.cookies.set(SESSION_COOKIE, sign_session("luomo"))
        assert tokens[0] not in client.get("/admin/subscriptions").text
        client.cookies.clear()
        with connect() as conn:
            conn.execute("UPDATE subscription_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(grants[1]["id"],))
            conn.commit()
        for fmt in formats:
            assert client.get(f"/sub/{tokens[1]}?format={fmt}").status_code == 404
            checks += 1
    # Check a cycle that follows a valid leaf; a short-circuit traversal misses it.
    broken = yaml.safe_load(baseline)
    broken["proxy-groups"] = [{"name":"PROXY","type":"select","proxies":["Public-测试","PROXY"]}]
    try:
        convert(yaml.safe_dump(broken).encode(), "clash")
        raise AssertionError("Cyclic profile accepted")
    except ConversionError:
        checks += 1
    print(json.dumps({"status":"PASS","scenario_groups":checks,"formats":formats,"identity_isolation":"PASS","revocation_expiry":"PASS","head":"PASS","node_interlock":"PASS"}))


if __name__ == "__main__":
    try:
        main()
    finally:
        temporary.cleanup()
