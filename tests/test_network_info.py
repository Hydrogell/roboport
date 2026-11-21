"""Тесты для app/network_info.py — детектор docker-bridge IP."""
import pytest

from app import network_info as ni


class TestIsDockerBridgeIp:
    @pytest.mark.parametrize("ip", [
        "172.17.0.1",     # стандартный docker0
        "172.17.0.2",
        "172.18.0.1",     # custom docker network
        "172.20.5.10",
        "172.31.255.254", # граница диапазона
    ])
    def test_docker_range(self, ip):
        assert ni._is_docker_bridge_ip(ip) is True

    @pytest.mark.parametrize("ip", [
        "192.168.1.1",
        "10.0.0.1",
        "192.168.50.42",  # обычный адрес локальной сети
        "172.15.0.1",     # ниже диапазона
        "172.32.0.1",     # выше диапазона
        "8.8.8.8",
        "127.0.0.1",
    ])
    def test_not_docker(self, ip):
        assert ni._is_docker_bridge_ip(ip) is False

    @pytest.mark.parametrize("ip", [
        "",
        "garbage",
        "172.",            # обрезанный
        "172.abc.0.1",     # не число во второй октете
        "::1",             # IPv6
    ])
    def test_garbage_safe(self, ip):
        # Не должны падать на мусоре — возвращаем False
        assert ni._is_docker_bridge_ip(ip) is False
