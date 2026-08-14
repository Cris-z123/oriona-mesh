"""可信来源 IP 解析单元测试（T030 / FR-026）。

覆盖：默认忽略转发头、可信代理多跳由右向左取首个非可信地址、非法链或全可信链回退
直连对端、不信任 X-Real-IP。
"""

import ipaddress

from app.infrastructure.rate_limit.source_ip import resolve_source_ip

TRUSTED = [ipaddress.ip_network("10.0.0.0/8")]


class TestSourceIpResolution:
    def test_no_trusted_proxies_ignores_forwarded_for(self) -> None:
        assert resolve_source_ip("203.0.113.9", "1.2.3.4", []) == "203.0.113.9"

    def test_untrusted_peer_ignores_forwarded_for(self) -> None:
        # 直连对端不是可信代理：不得信任任何转发头。
        assert resolve_source_ip("203.0.113.9", "6.6.6.6", TRUSTED) == "203.0.113.9"

    def test_single_hop_picks_first_untrusted(self) -> None:
        # 链 "client, proxy"：由右向左跳过可信代理，取首个非可信地址。
        assert resolve_source_ip("10.0.0.1", "203.0.113.9, 10.0.0.1", TRUSTED) == "203.0.113.9"

    def test_multi_hop_right_to_left(self) -> None:
        chain = "203.0.113.9, 10.0.0.1, 10.0.0.2"
        assert resolve_source_ip("10.0.0.2", chain, TRUSTED) == "203.0.113.9"

    def test_illegal_chain_falls_back_to_peer(self) -> None:
        # 链中出现非法地址：回退直连对端，不得导致限流旁路。
        assert resolve_source_ip("10.0.0.1", "203.0.113.9, not-an-ip", TRUSTED) == "10.0.0.1"
        assert resolve_source_ip("10.0.0.1", "203.0.113.9,,10.0.0.2", TRUSTED) == "10.0.0.1"

    def test_all_trusted_chain_falls_back_to_peer(self) -> None:
        assert resolve_source_ip("10.0.0.3", "10.0.0.1, 10.0.0.2", TRUSTED) == "10.0.0.3"

    def test_empty_or_missing_forwarded_for_falls_back_to_peer(self) -> None:
        assert resolve_source_ip("10.0.0.1", None, TRUSTED) == "10.0.0.1"
        assert resolve_source_ip("10.0.0.1", "", TRUSTED) == "10.0.0.1"

    def test_ipv6_peer_and_chain(self) -> None:
        v6_trusted = [ipaddress.ip_network("fd00::/8")]
        assert resolve_source_ip("fd00::1", "2001:db8::5, fd00::1", v6_trusted) == "2001:db8::5"
        assert resolve_source_ip("fd00::1", "2001:db8::5", []) == "fd00::1"

    def test_x_real_ip_never_trusted(self) -> None:
        # 解析器只消费 X-Forwarded-For；X-Real-IP 不参与（中间件也只传入 XFF）。
        # 即使转发链缺失，也不从其他头推断客户端地址。
        assert resolve_source_ip("10.0.0.1", None, TRUSTED) == "10.0.0.1"

    def test_peer_ip_illegal_returns_raw_value(self) -> None:
        # 极端输入下不得抛异常；返回原值避免绕过。
        assert resolve_source_ip("not-an-ip", "203.0.113.9", TRUSTED) == "not-an-ip"
