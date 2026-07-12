import os

import httpx

BASE = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8791")


def main() -> None:
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        home = client.get(f"{BASE}/")
        assert home.status_code == 200
        text = home.text
        assert "LuomoFile Hub" in text
        assert "Storage Backends" not in text
        assert "Recent Files" not in text
        assert "Active Files" not in text
        assert "Stored Bytes" not in text
        assert "local_temp" not in text
        assert ">Upload<" not in text
        assert ">Temp<" not in text
        assert ">Images<" not in text
        assert ">Admin<" not in text
        assert ">Login<" in text
        assert ">Register<" in text

        upload = client.get(f"{BASE}/upload")
        assert upload.status_code == 200
        assert 'id="upload-form"' not in upload.text
        assert "Sign in required" in upload.text

        temp = client.get(f"{BASE}/temp")
        assert temp.status_code == 200
        assert 'id="upload-form"' not in temp.text
        assert "Sign in required" in temp.text
    print("public pages smoke ok")


if __name__ == "__main__":
    main()
