"""可信来源 IP 解析器（FR-026 / data-model 基础设施数据边界）。

规则：
- 默认忽略全部转发头，使用 TCP 直连对端 IP；
- 仅当直连对端命中显式可信代理 CIDR 时，才解析 ``X-Forwarded-For``，并由右向左
  选择首个非可信地址；
- 转发链任一段格式非法，或不存在非可信地址时，回退直连对端；
- 不信任 ``X-Real-IP`` 或任何其他转发头；
- 完整转发链不得写入 Redis、日志或指标（本解析器只返回单个解析结果）。
"""

import ipaddress
from collections.abc import Sequence

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def resolve_source_ip(
    peer_ip: str, forwarded_for: str | None, trusted_networks: Sequence[Network]
) -> str:
    """返回用于认证限流计数的来源 IP。

    :param peer_ip: TCP 直连对端（请求方）IP 字符串。
    :param forwarded_for: ``X-Forwarded-For`` 头原始值；None 表示缺失。
    :param trusted_networks: 显式可信代理 CIDR；为空时忽略全部转发头。
    """
    peer = _parse_address(peer_ip)
    if peer is None:
        # 对端 IP 本身非法（正常不会发生）：回退原值，避免伪造绕过。
        return peer_ip
    if not trusted_networks or not _in_networks(peer, trusted_networks):
        return str(peer)
    if not forwarded_for:
        return str(peer)

    parts = [p.strip() for p in forwarded_for.split(",")]
    for raw in reversed(parts):
        if not raw:
            # 空段视为转发链格式非法：回退直连对端，不得导致限流旁路。
            return str(peer)
        address = _parse_address(raw)
        if address is None:
            # 转发链格式非法：回退直连对端，不得导致限流旁路。
            return str(peer)
        if not _in_networks(address, trusted_networks):
            return str(address)
    # 全部为可信地址或链为空：回退直连对端。
    return str(peer)


def _parse_address(value: str) -> Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _in_networks(address: Address, networks: Sequence[Network]) -> bool:
    return any(address in network for network in networks)
