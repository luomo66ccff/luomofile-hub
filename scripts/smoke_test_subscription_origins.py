"""Exercise the source boundary without real sessions, credentials, or storage."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["APP_BASE_URL"] = "https://file.example.test"
os.environ["APP_ENV"] = "test"

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from app.security import SESSION_COOKIE, security_headers


def main():
    app = FastAPI()
    app.middleware("http")(security_headers)
    executed = []

    @app.post("/admin/subscriptions/create")
    def create():
        executed.append(True)
        return Response(status_code=202)

    @app.get("/admin/subscriptions")
    @app.get("/verify-email")
    @app.get("/sub/test-only-invalid-token")
    @app.get("/admin/api-keys")
    def read():
        return Response("test")

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, "test-only-cookie")
    accepted = [
        {"origin": "https://file.example.test"},
        {"origin": "https://file.example.test:443", "sec-fetch-site": "same-origin"},
        {"origin": "https://FILE.EXAMPLE.TEST"},
        {"referer": "https://file.example.test/admin/subscriptions"},
    ]
    rejected = [
        {}, {"sec-fetch-site": "same-origin"},
        {"origin": "null", "referer": "https://file.example.test/admin/subscriptions"},
        {"origin": "", "referer": "https://file.example.test/admin/subscriptions"},
        {"origin": "https://attacker.example"},
        {"origin": "https://file.example.test", "sec-fetch-site": "cross-site"},
        {"origin": "http://file.example.test"},
        {"origin": "https://file.example.test:444"},
        {"origin": "https://file.example.test:0"},
        {"origin": "https://file.example.test:65536"},
        {"origin": "https://file.example.test:not-a-port"},
        {"origin": "https://file.example.test:"},
        {"origin": "https://file.example.test/path"},
        {"origin": "https://file.example.test?query=1"},
        {"origin": "https://file.example.test#fragment"},
        {"origin": "https://user@file.example.test"},
        {"origin": "https://@file.example.test"},
        {"origin": "https://[broken"},
        {"origin": "https://attacker.example", "host": "attacker.example", "x-forwarded-host": "attacker.example", "x-forwarded-proto": "https"},
        {"origin": "https://attacker.example", "forwarded": "host=file.example.test;proto=https", "x-forwarded-host": "file.example.test"},
        {"referer": "https://attacker.example/admin/subscriptions"},
        [("origin", "https://file.example.test"), ("origin", "https://attacker.example")],
    ]
    for headers in accepted:
        assert client.post("/admin/subscriptions/create", headers=headers).status_code == 202
    for headers in rejected:
        response = client.post("/admin/subscriptions/create", headers=headers)
        assert response.status_code == 403, headers
        assert "返回订阅管理" in response.text and "location" not in response.headers
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "max-age=" in response.headers["strict-transport-security"]
    assert len(executed) == len(accepted), "Rejected requests reached the mutation route."
    response = client.post("/admin/subscriptions/create", headers={"origin": "null", "accept": "application/json"})
    assert response.status_code == 403 and response.json()["error"] == "cross_origin_rejected"
    assert client.get("/admin/subscriptions").headers["referrer-policy"] == "same-origin"
    for path in ["/verify-email?token=test-only", "/sub/test-only-invalid-token", "/admin/api-keys?created_key=test-only", "/admin/subscriptions?token=test-only"]:
        response = client.get(path)
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
    print("SUBSCRIPTION_ORIGINS_SMOKE=PASS (source matrix, early rejection headers, token page policies)")


if __name__ == "__main__":
    main()
