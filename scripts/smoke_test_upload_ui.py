import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.db import connect, init_db
from app.security import SESSION_COOKIE, hash_password, now_iso, sign_session

BASE = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8791")


def main() -> None:
    init_db()
    username = _ensure_active_user()
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        denied = client.post(
            f"{BASE}/upload",
            files={"file": ("blocked.txt", b"blocked", "text/plain")},
            data={"storage_policy": "local_temp"},
        )
        assert denied.status_code in {401, 403}, denied.text

        client.cookies.set(SESSION_COOKIE, sign_session(username))
        page = client.get(f"{BASE}/upload")
        assert page.status_code == 200, page.text
        text = page.text
        assert "upload-dropzone" in text
        assert text.count('type="file"') == 1
        assert "multiple" in text
        assert 'id="upload-button" class="btn primary" type="button"' in text
        file_input_pos = text.index('type="file"')
        file_input_close = text.index(">", file_input_pos)
        assert "required" not in text[file_input_pos:file_input_close].lower()
        assert "/static/upload-enhance.js?v=upload-flow-20260620" in text
        assert 'src="/static/upload-enhance.js?v=upload-flow-20260620" defer' in text
        assert "/static/upload-enhance.css?v=upload-flow-20260620" in text
        assert 'action="/upload"' in text
        checkbox_pos = text.index('name="generate_link"')
        next_close = text.index(">", checkbox_pos)
        assert "checked" not in text[checkbox_pos:next_close].lower()
        lowered = text.lower()
        for secret_word in ("session_secret", "r2_secret", "cos_secret", "internal_token", "api key"):
            assert secret_word not in lowered

        image_page = client.get(f"{BASE}/upload?purpose=image")
        assert image_page.status_code == 200
        assert '<option value="image" selected' in image_page.text

        temp_page = client.get(f"{BASE}/temp")
        assert temp_page.status_code == 200
        assert 'value="temp"' in temp_page.text

        js = client.get(f"{BASE}/static/upload-enhance.js")
        css = client.get(f"{BASE}/static/upload-enhance.css")
        assert js.status_code == 200 and "dataTransfer.files" in js.text
        assert "DOMContentLoaded" in js.text
        assert 'uploadButton.addEventListener("click", handleUploadClick)' in js.text
        assert "selectedFiles.length" in js.text
        assert 'data.append("file", file, file.name)' in js.text
        assert 'form.getAttribute("action") || window.location.pathname' in js.text
        assert 'console.info("[LuomoFile] uploading to:", url)' in js.text
        assert 'setRowStatus(index, "Uploading...", 1)' in js.text
        assert "Preparing upload..." in js.text and "Uploading..." in js.text and "Uploaded" in js.text
        assert "Please choose one or more files." in js.text
        assert "event.preventDefault();" in js.text
        assert css.status_code == 200 and ".upload-dropzone" in css.text
    print("upload ui smoke ok")


def _ensure_active_user() -> str:
    username = "upload_ui_smoke"
    now = now_iso()
    with connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO users(username, email, password_hash, role, status, created_at, updated_at)
                VALUES (?, ?, ?, 'developer', 'active', ?, ?)
                """,
                (username, "upload-ui-smoke@example.invalid", hash_password("SmokePassword123"), now, now),
            )
            conn.commit()
    return username


if __name__ == "__main__":
    main()
