import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import settings


class IdentitySyncError(RuntimeError):
    pass


def _targets() -> list[tuple[str, str]]:
    result = []
    for item in settings.subscription_identity_targets.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, address = item.partition("=")
        if not separator or not name.strip() or not address.strip():
            raise IdentitySyncError("Invalid SUBSCRIPTION_IDENTITY_TARGETS entry.")
        result.append((name.strip(), address.strip()))
    if not result:
        raise IdentitySyncError("No subscription identity targets are configured.")
    return result


def _sync_target(
    name: str,
    address: str,
    payload: str,
    key_path: Path,
    known_hosts_path: Path,
) -> tuple[dict | None, str | None]:
    target = address if "@" in address else f"root@{address}"
    command = [
        "/usr/bin/ssh",
        "-F", "/dev/null",
        "-i", str(key_path),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts_path}",
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        target,
        "sync",
    ]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            text=True,
            capture_output=True,
            timeout=settings.subscription_identity_sync_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{name}:{type(exc).__name__}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "remote failure").strip().replace("\n", " ")[-240:]
        return None, f"{name}:{detail}"
    try:
        remote = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, f"{name}:invalid response"
    if remote.get("ok") is not True:
        return None, f"{name}:remote rejected update"
    return {"target": name, **remote}, None


def sync_identity_records(records: list[dict]) -> list[dict]:
    if not settings.subscription_identity_sync_enabled:
        if settings.app_env == "test":
            return [{"target": "test", "ok": True, "identities": len(records)}]
        raise IdentitySyncError("Node identity synchronization is disabled in production.")

    key_path = Path(settings.subscription_identity_ssh_key)
    known_hosts_path = Path(settings.subscription_identity_known_hosts)
    if not key_path.is_file() or not known_hosts_path.is_file():
        raise IdentitySyncError("Node identity SSH material is unavailable.")

    payload = json.dumps(
        {"version": 2, "identities": records},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    targets = _targets()
    by_name = {}
    failures = []
    with ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="identity-sync") as executor:
        futures = {
            executor.submit(_sync_target, name, address, payload, key_path, known_hosts_path): name
            for name, address in targets
        }
        for future in as_completed(futures):
            result, failure = future.result()
            if failure:
                failures.append(failure)
            elif result:
                by_name[result["target"]] = result

    if failures:
        raise IdentitySyncError("; ".join(sorted(failures)))
    return [by_name[name] for name, _address in targets]
