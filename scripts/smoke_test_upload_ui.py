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
        assert 'id="upload-button" class="btn btn-primary" type="submit"' in text
        file_input_pos = text.index('type="file"')
        file_input_close = text.index(">", file_input_pos)
        assert "required" in text[file_input_pos:file_input_close].lower()
        assert "/static/js/harbor.js?v=" in text
        assert 'type="module" src="/static/js/harbor.js?v=' in text
        assert "/static/css/harbor.css?v=" in text and "/static/css/pages.css?v=" in text
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

        js = client.get(f"{BASE}/static/js/upload.js")
        css = client.get(f"{BASE}/static/css/harbor.css")
        assert js.status_code == 200 and "e.dataTransfer.files" in js.text
        assert "new XMLHttpRequest()" in js.text and 'xhr.open("POST",task.url)' in js.text
        assert 'xhr.setRequestHeader("Accept","application/json")' in js.text
        assert 'fields.set("csrf_token",document.body.dataset.csrf)' in js.text
        assert 'data.append(task.fileField,task.file)' in js.text
        assert 'task.row.dataset.state="uploading"' in js.text
        assert 'task.row.dataset.state="done"' in js.text and 'task.row.dataset.state="error"' in js.text
        assert "selected.length>20" in js.text and 'xhr.upload.onprogress' in js.text
        assert 'input.files.length' in js.text and 'e.preventDefault()' in js.text
        assert 'queuedForm(form)' in js.text and 'new FormData(form,submitter)' in js.text
        assert '"/upload","/images/upload","/temp"' in js.text
        assert 'fileField=form?.querySelector' in js.text and 'modalOpen()' in js.text
        assert css.status_code == 200 and ".dock-overlay" in css.text and ".tray" in css.text
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
