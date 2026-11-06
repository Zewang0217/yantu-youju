"""采集应用的公开来源白名单；与HTML发现候选区分，拒绝越界访问。"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def validate_source_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """仅接受配置主机的HTTPS默认端口，拒绝凭据/控制字符及非公开IP字面量。

    不进行DNS探测；DNS重绑定等网络层限制需由实际部署的出口策略补充。
    """
    try:
        if not isinstance(url, str) or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
            raise ValueError("URL含空白或控制字符")
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or host.lower() not in {h.lower() for h in allowed_hosts}
                or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError("URL不在配置的公开HTTPS来源范围")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("不允许访问非公开IP来源")
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit(("https", netloc.lower(), parsed.path or "/", parsed.query, ""))
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError("来源URL校验失败") from e
