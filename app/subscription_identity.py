import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass

from app.config import settings


MANAGED_ID_PREFIX = "lsub-"
UNAVAILABLE_PUBLIC_LEAF_NAMES = frozenset(
    {
        "Tokyo1-TUIC-Public-日本东京1",
        "Tokyo1-HY2-Public-日本东京1",
        "Tokyo1-Direct-SS-日本东京1",
        "Singapore-TUIC-Public-新加坡",
        "Singapore-HY2-Public-新加坡",
        "Singapore-Direct-SS-新加坡",
        "SiliconValley-TUIC-Public-美国硅谷",
        "SiliconValley-HY2-Public-美国硅谷",
        "SiliconValley-Direct-SS-美国硅谷",
    }
)


@dataclass(frozen=True)
class SubscriptionIdentity:
    user_id: str
    vmess_uuid: str
    hysteria2_password: str
    tuic_uuid: str
    tuic_password: str
    shadowsocks_password: str

    def as_record(self) -> dict:
        return {
            "user_id": self.user_id,
            "vmess_uuid": self.vmess_uuid,
            "hysteria2_password": self.hysteria2_password,
            "tuic_uuid": self.tuic_uuid,
            "tuic_password": self.tuic_password,
            "shadowsocks_password": self.shadowsocks_password,
        }


def _secret() -> bytes:
    value = settings.subscription_identity_secret.encode("utf-8")
    if len(value) < 32:
        raise RuntimeError("SUBSCRIPTION_IDENTITY_SECRET must contain at least 32 bytes.")
    return value


def _digest(token_hash: str, purpose: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", token_hash):
        raise ValueError("Invalid subscription token hash.")
    message = f"luomofile-subscription-identity-v2:{purpose}:{token_hash}".encode("ascii")
    return hmac.new(_secret(), message, hashlib.sha256).digest()


def _uuid_from_digest(value: bytes) -> str:
    raw = bytearray(value[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _password(token_hash: str, purpose: str, length: int = 32) -> str:
    value = base64.urlsafe_b64encode(_digest(token_hash, purpose)).decode("ascii").rstrip("=")
    return value[:length]


def derive_subscription_identity(token_hash: str) -> SubscriptionIdentity:
    return SubscriptionIdentity(
        user_id=MANAGED_ID_PREFIX + token_hash[:20],
        vmess_uuid=_uuid_from_digest(_digest(token_hash, "vmess-uuid")),
        hysteria2_password=_password(token_hash, "hysteria2-password"),
        tuic_uuid=_uuid_from_digest(_digest(token_hash, "tuic-uuid")),
        tuic_password=_password(token_hash, "tuic-password"),
        shadowsocks_password=_password(token_hash, "shadowsocks-password"),
    )


def _replace_yaml_scalar(line: str, key: str, value: str) -> str:
    match = re.match(rf"^(\s*){re.escape(key)}\s*:(.*)$", line)
    if not match:
        return line
    return f"{match.group(1)}{key}: {json.dumps(value, ensure_ascii=False)}"


def _proxy_block_name(lines: list[str], left: int, right: int) -> str:
    for index in range(left, right):
        match = re.match(r"^- name:\s*(.*?)\s*$", lines[index])
        if not match:
            continue
        value = match.group(1)
        if value.startswith('"'):
            try:
                return str(json.loads(value))
            except json.JSONDecodeError:
                return value.strip('"')
        return value.strip("'")
    return ""


def _yaml_list_item_value(line: str) -> str:
    match = re.match(r"^\s*-\s*(.*?)\s*$", line)
    if not match:
        return ""
    value = match.group(1)
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except json.JSONDecodeError:
            return value.strip('"')
    return value.strip("'")


def render_subscription_payload(payload: bytes, identity: SubscriptionIdentity) -> bytes:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Subscription template must be UTF-8 YAML.") from exc

    try:
        start = lines.index("proxies:") + 1
    except ValueError as exc:
        raise ValueError("Subscription template has no proxies section.") from exc

    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "-")):
            end = index
            break

    block_starts = [index for index in range(start, end) if re.match(r"^- name:\s*", lines[index])]
    block_starts.append(end)
    kept_proxy_lines = []
    for block_index in range(len(block_starts) - 1):
        left, right = block_starts[block_index], block_starts[block_index + 1]
        if _proxy_block_name(lines, left, right) in UNAVAILABLE_PUBLIC_LEAF_NAMES:
            continue
        kept_proxy_lines.extend(lines[left:right])
    lines = lines[:start] + kept_proxy_lines + lines[end:]

    end = start + len(kept_proxy_lines)
    block_starts = [index for index in range(start, end) if re.match(r"^- name:\s*", lines[index])]
    block_starts.append(end)
    counts = {"vmess": 0, "hysteria2": 0, "tuic": 0, "ss": 0}

    for block_index in range(len(block_starts) - 1):
        left, right = block_starts[block_index], block_starts[block_index + 1]
        proxy_type = ""
        for index in range(left + 1, right):
            match = re.match(r"^  type:\s*([A-Za-z0-9_-]+)\s*$", lines[index])
            if match:
                proxy_type = match.group(1)
                break
        if proxy_type not in counts:
            continue

        replaced = 0
        for index in range(left + 1, right):
            stripped = lines[index].lstrip()
            if proxy_type == "vmess" and stripped.startswith("uuid:"):
                lines[index] = _replace_yaml_scalar(lines[index], "uuid", identity.vmess_uuid)
                replaced += 1
            elif proxy_type == "hysteria2" and stripped.startswith("password:"):
                lines[index] = _replace_yaml_scalar(lines[index], "password", identity.hysteria2_password)
                replaced += 1
            elif proxy_type == "tuic" and stripped.startswith("uuid:"):
                lines[index] = _replace_yaml_scalar(lines[index], "uuid", identity.tuic_uuid)
                replaced += 1
            elif proxy_type == "tuic" and stripped.startswith("password:"):
                lines[index] = _replace_yaml_scalar(lines[index], "password", identity.tuic_password)
                replaced += 1
            elif proxy_type == "ss" and stripped.startswith("password:"):
                lines[index] = _replace_yaml_scalar(lines[index], "password", identity.shadowsocks_password)
                replaced += 1

        expected = 2 if proxy_type == "tuic" else 1
        if replaced != expected:
            raise ValueError(f"Subscription {proxy_type} node has an unexpected credential shape.")
        counts[proxy_type] += 1

    missing = [name for name, count in counts.items() if count == 0]
    if missing:
        raise ValueError("Subscription template is missing revocable node types: " + ", ".join(missing))
    lines = lines[:end] + [
        line for line in lines[end:]
        if _yaml_list_item_value(line) not in UNAVAILABLE_PUBLIC_LEAF_NAMES
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")
