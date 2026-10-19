"""Convert a personalized subscription after authorization and identity rendering."""
from __future__ import annotations

import base64
from copy import deepcopy
import ipaddress
import json
from urllib.parse import quote

import yaml

VMESS_KEYS = {"name", "type", "server", "port", "uuid", "alterId", "cipher", "udp", "tls", "servername", "skip-cert-verify", "network", "ws-opts"}
SS_KEYS = {"name", "type", "server", "port", "cipher", "password", "udp"}
GROUP_KEYS = {"name", "type", "proxies", "url", "interval", "tolerance", "lazy", "strategy"}
PROBE_URL = "https://cp.cloudflare.com/generate_204"


class ConversionError(ValueError):
    pass


FORMAT_OPTIONS = {
    "mihomo": {"label": "Mihomo / Clash Verge（完整）", "media_type": "text/yaml; charset=utf-8", "filename": "subscription.yaml", "description": "保留完整节点；私网节点需要 ZeroTier。"},
    "clash": {"label": "Clash for Windows（旧版）", "media_type": "text/yaml; charset=utf-8", "filename": "subscription-clash.yaml", "description": "适配旧内核的公网节点与分流配置。"},
    "base64": {"label": "v2rayN / v2rayNG / Shadowrocket", "media_type": "text/plain; charset=utf-8", "filename": "subscription-base64.txt", "description": "通用节点订阅，分流规则由客户端管理。"},
    "uri": {"label": "通用分享链接", "media_type": "text/plain; charset=utf-8", "filename": "subscription-uris.txt", "description": "逐行节点链接，适合批量导入。"},
    "sing-box": {"label": "sing-box 1.12+（桌面代理）", "media_type": "application/json", "filename": "subscription-sing-box.json", "description": "本机 HTTP / SOCKS 入口，不含手机 VPN 接管和中国 IP 分流规则。"},
}


def parse_format_options(query) -> tuple[str, bool]:
    for key in ("format", "include_private"):
        if len(query.getlist(key)) > 1:
            raise ConversionError("Duplicate format option")
    target = query.get("format", "mihomo")
    include_private = query.get("include_private", "0")
    if target not in FORMAT_OPTIONS or include_private not in {"0", "1"}:
        raise ConversionError("Unsupported format option")
    return target, include_private == "1"


def is_private_server(server: str) -> bool:
    try:
        return not ipaddress.ip_address(server).is_global
    except ValueError:
        return server.lower().endswith((".internal", ".local", ".localhost")) or server.lower() == "localhost"


def select_nodes(config: dict, include_private: bool = False) -> tuple[list[dict], list[dict]]:
    nodes, omitted = [], []
    names = set()
    for original in config.get("proxies", []):
        name = original.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ConversionError("Missing or duplicate node name")
        names.add(name)
        protocol = original.get("type")
        reason = None
        if not include_private and is_private_server(str(original.get("server", ""))):
            reason = "private_network_requires_ZeroTier"
        elif protocol not in {"vmess", "ss"}:
            reason = "protocol_excluded_from_legacy_profile"
        elif protocol == "vmess" and original.get("network", "tcp") not in {"tcp", "ws"}:
            reason = "transport_not_implemented"
        elif any(original.get(key) for key in ("plugin", "plugin-opts", "smux", "reality-opts", "shadow-tls-opts", "restls-opts", "dialer-proxy", "packet-encoding")):
            reason = "required_extension_not_implemented"
        if reason:
            omitted.append({"name": name, "type": protocol, "reason": reason})
            continue
        required = ("server", "port", "uuid", "cipher") if protocol == "vmess" else ("server", "port", "password", "cipher")
        if any(key not in original or original[key] in (None, "") for key in required):
            raise ConversionError("Node is missing a required field")
        if type(original["port"]) is not int or not 1 <= original["port"] <= 65535:
            raise ConversionError("Invalid node port")
        if protocol == "ss" and original["cipher"] not in {"aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305"}:
            omitted.append({"name": name, "type": protocol, "reason": "cipher_not_implemented"})
            continue
        if protocol == "vmess" and original.get("network", "tcp") == "ws":
            ws = original.get("ws-opts", {})
            if set(ws) - {"path", "headers"}:
                raise ConversionError("WebSocket extension needs explicit conversion")
        allowed = VMESS_KEYS if protocol == "vmess" else SS_KEYS
        node = {key: deepcopy(value) for key, value in original.items() if key in allowed}
        if protocol == "vmess":
            node.setdefault("alterId", 0)
        nodes.append(node)
    if not nodes:
        raise ConversionError("No compatible nodes remain; refusing to emit a DIRECT-only profile")
    return nodes, omitted


def legacy_clash(config: dict, nodes: list[dict]) -> dict:
    node_names = {node["name"] for node in nodes}
    raw_groups = config.get("proxy-groups", [])
    groups = {item["name"]: item for item in raw_groups}
    if len(groups) != len(raw_groups) or node_names.intersection(groups) or node_names.intersection({"DIRECT", "REJECT"}):
        raise ConversionError("Duplicate or reserved policy name")

    complete = set()
    def check_cycles(name: str, visiting: frozenset = frozenset()) -> None:
        if name in visiting:
            raise ConversionError("Policy group cycle")
        if name in complete or name not in groups:
            return
        for ref in groups[name].get("proxies", []):
            check_cycles(ref, visiting | {name})
        complete.add(name)
    for name in groups:
        check_cycles(name)

    def has_node(name: str, visiting: frozenset = frozenset()) -> bool:
        if name in node_names:
            return True
        if name in visiting:
            raise ConversionError("Policy group cycle")
        if name not in groups:
            return False
        return any(has_node(ref, visiting | {name}) for ref in groups[name].get("proxies", []))

    retained = {name for name in groups if has_node(name)}
    valid = node_names | retained | {"DIRECT", "REJECT"}
    result_groups = []
    for original in raw_groups:
        if original["name"] not in retained:
            continue
        if original["type"] not in {"select", "url-test", "fallback", "load-balance"}:
            raise ConversionError("Unsupported group type")
        group = {k: deepcopy(v) for k, v in original.items() if k in GROUP_KEYS}
        group["proxies"] = [name for name in group.get("proxies", []) if name in valid]
        if not group["proxies"]:
            raise ConversionError("Empty policy group")
        result_groups.append(group)
    if "PROXY" not in valid:
        result_groups.insert(0, {"name": "PROXY", "type": "select", "proxies": [p["name"] for p in nodes]})
        valid.add("PROXY")
    rules = deepcopy(config.get("rules", ["MATCH,PROXY"]))
    for rule in rules:
        parts = rule.split(",")
        policy = parts[-2] if parts[-1] == "no-resolve" else parts[-1]
        if policy not in valid:
            raise ConversionError("Rule references an unavailable policy")
    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": ["*.lan", "localhost"],
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": ["223.5.5.5", "119.29.29.29"],
        },
        "proxies": deepcopy(nodes),
        "proxy-groups": result_groups,
        "rules": rules,
    }


def uri_lines(nodes: list[dict]) -> str:
    links = []
    for node in nodes:
        if node["type"] == "vmess":
            ws = node.get("ws-opts", {})
            headers = ws.get("headers", {})
            if any(key.lower() != "host" for key in headers):
                raise ConversionError("VMess URI cannot retain custom WebSocket headers")
            if node.get("skip-cert-verify"):
                raise ConversionError("VMess URI cannot portably retain insecure TLS")
            host = next((str(v) for k, v in headers.items() if k.lower() == "host"), "")
            item = {
                "v": "2", "ps": node["name"], "add": node["server"], "port": str(node["port"]),
                "id": node["uuid"], "aid": str(node["alterId"]), "scy": node["cipher"],
                "net": node.get("network", "tcp"), "type": "none", "host": host,
                "path": ws.get("path", "") if node.get("network") == "ws" else "",
                "tls": "tls" if node.get("tls") else "", "sni": node.get("servername", ""),
            }
            links.append("vmess://" + base64.b64encode(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()).decode())
        else:
            auth = base64.urlsafe_b64encode(f"{node['cipher']}:{node['password']}".encode()).decode().rstrip("=")
            host = f"[{node['server']}]" if ":" in node["server"] else node["server"]
            links.append(f"ss://{auth}@{host}:{node['port']}#{quote(node['name'], safe='')}")
    return "\n".join(links) + "\n"


def singbox(nodes: list[dict]) -> dict:
    names = [node["name"] for node in nodes]
    if set(names).intersection({"PROXY", "AUTO", "DIRECT"}):
        raise ConversionError("Reserved sing-box policy name")
    outbounds = [
        {"type": "selector", "tag": "PROXY", "outbounds": ["AUTO", *names, "DIRECT"], "default": "AUTO"},
        {"type": "urltest", "tag": "AUTO", "outbounds": names, "url": PROBE_URL, "interval": "3m", "tolerance": 150},
        {"type": "direct", "tag": "DIRECT"},
    ]
    for node in nodes:
        outbound = {"type": "vmess" if node["type"] == "vmess" else "shadowsocks", "tag": node["name"], "server": node["server"], "server_port": node["port"]}
        if node.get("udp") is False:
            outbound["network"] = "tcp"
        if node["type"] == "ss":
            outbound.update(method=node["cipher"], password=node["password"])
        else:
            outbound.update(uuid=node["uuid"], security=node["cipher"], alter_id=int(node["alterId"]), packet_encoding="")
            if node.get("tls"):
                outbound["tls"] = {"enabled": True, "server_name": node.get("servername", node["server"]), "insecure": bool(node.get("skip-cert-verify", False))}
            if node.get("network") == "ws":
                ws = node.get("ws-opts", {})
                outbound["transport"] = {"type": "ws", "path": ws.get("path", "/"), "headers": deepcopy(ws.get("headers", {}))}
        outbounds.append(outbound)
    return {
        "log": {"level": "warn"},
        "dns": {"servers": [
            {"type": "udp", "tag": "bootstrap", "server": "223.5.5.5"},
            {"type": "https", "tag": "remote", "server": "1.1.1.1", "detour": "PROXY"},
        ], "final": "remote", "strategy": "ipv4_only"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 7890}],
        "outbounds": outbounds,
        "route": {
            "default_domain_resolver": "bootstrap",
            "rules": [
                {"ip_is_private": True, "action": "route", "outbound": "DIRECT"},
                {"port": [25565, 19132, 19133, 25575], "action": "route", "outbound": "DIRECT"},
            ],
            "final": "PROXY",
        },
    }


def convert(payload: bytes, target: str, include_private: bool = False) -> tuple[bytes, dict]:
    try:
        config = yaml.safe_load(payload)
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConversionError("Invalid subscription YAML") from exc
    if not isinstance(config, dict) or not isinstance(config.get("proxies"), list):
        raise ConversionError("Expected a rendered Clash subscription with nodes, not HTML")
    nodes, omitted = select_nodes(config, include_private)
    if target == "clash":
        output = yaml.safe_dump(legacy_clash(config, nodes), allow_unicode=True, sort_keys=False).encode()
    elif target in {"uri", "base64"}:
        output = uri_lines(nodes).encode()
        if target == "base64":
            output = base64.b64encode(output) + b"\n"
    elif target == "sing-box":
        output = (json.dumps(singbox(nodes), ensure_ascii=False, indent=2) + "\n").encode()
    else:
        raise ConversionError("Unknown format")
    return output, {"format": target, "included": len(nodes), "omitted": omitted, "include_private": include_private}

