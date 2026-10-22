"""Wake-on-LAN (port of WakeOnLAN.swift): magic packets (6 × 0xFF, then the MAC address 16 times)
as UDP broadcasts."""

from __future__ import annotations

import socket

#: The limited broadcast. It leaves through the interface of the default route only; pass subnet
#: broadcasts (e.g. "192.168.1.255") to reach other networks.
LIMITED_BROADCAST = "255.255.255.255"
DEFAULT_PORT = 9

_SEPARATORS = ":-."
_HEX = set("0123456789abcdefABCDEF")


class WakeOnLANError(Exception):
    """kind: "invalidMACAddress", "invalidBroadcastAddress" (value: the text) or "socket" (value: errno)."""

    def __init__(self, kind: str, value: str | int):
        super().__init__(kind, value)
        self.kind = kind
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WakeOnLANError) and (self.kind, self.value) == (other.kind, other.value)

    def __hash__(self) -> int:
        return hash((self.kind, self.value))


def mac_address(text: str) -> bytes | None:
    """Parses "00:11:22:33:44:55", "00-11-22-33-44-55" or "001122334455" (any case)."""
    digits = "".join(ch for ch in text.strip(" \t") if ch not in _SEPARATORS)
    if len(digits) != 12 or not set(digits) <= _HEX:
        return None
    return bytes.fromhex(digits)


def magic_packet(mac: bytes) -> bytes:
    if len(mac) != 6:
        raise ValueError("a MAC address has 6 bytes")
    return b"\xff" * 6 + mac * 16


def wake(mac_addresses: list[str], broadcast_addresses: list[str] | None = None, port: int = DEFAULT_PORT) -> None:
    """Sends one magic packet per MAC address to each broadcast address. Raises before sending
    anything if an address is invalid."""
    packets = []
    for text in mac_addresses:
        mac = mac_address(text)
        if mac is None:
            raise WakeOnLANError("invalidMACAddress", text)
        packets.append(magic_packet(mac))
    destinations = []
    for text in broadcast_addresses or [LIMITED_BROADCAST]:
        try:
            socket.inet_pton(socket.AF_INET, text)
        except OSError:
            raise WakeOnLANError("invalidBroadcastAddress", text) from None
        destinations.append((text, port))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for packet in packets:
                for destination in destinations:
                    if sock.sendto(packet, destination) != len(packet):
                        raise WakeOnLANError("socket", 0)
    except OSError as error:
        raise WakeOnLANError("socket", error.errno or 0) from None
