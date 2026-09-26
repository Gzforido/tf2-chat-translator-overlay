"""Определение локального IPv4-адреса, на котором TF2 слушает RCON."""

import ctypes
import ipaddress
import socket
import sys
from ctypes import wintypes


ERROR_BUFFER_OVERFLOW = 111
ERROR_NO_DATA = 232

GAA_FLAG_SKIP_ANYCAST = 0x0002
GAA_FLAG_SKIP_MULTICAST = 0x0004
GAA_FLAG_SKIP_DNS_SERVER = 0x0008
GAA_FLAG_INCLUDE_ALL_INTERFACES = 0x0100

INITIAL_BUFFER_SIZE = 15 * 1024
MAX_BUFFER_RETRIES = 5


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
]

_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
]


class _SOCKADDR_IN(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


def find_tf2_rcon_host(port: int = 27015) -> str:
    """Вернуть локальный IPv4-адрес с доступным RCON-портом."""
    for ip in _local_ips():
        try:
            with socket.create_connection((ip, port), timeout=0.5):
                return ip
        except OSError:
            pass
    return "localhost"


def _local_ips() -> list[str]:
    """Вернуть IPv4-адреса всех интерфейсов, включая VPN-адаптеры."""
    if sys.platform != "win32":
        return _fallback_local_ips()

    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
        get_adapters_addresses = iphlpapi.GetAdaptersAddresses
        get_adapters_addresses.argtypes = [
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            ctypes.POINTER(_IP_ADAPTER_ADDRESSES),
            ctypes.POINTER(wintypes.ULONG),
        ]
        get_adapters_addresses.restype = wintypes.ULONG

        flags = (
            GAA_FLAG_SKIP_ANYCAST
            | GAA_FLAG_SKIP_MULTICAST
            | GAA_FLAG_SKIP_DNS_SERVER
            | GAA_FLAG_INCLUDE_ALL_INTERFACES
        )

        buffer_size = wintypes.ULONG(INITIAL_BUFFER_SIZE)

        for _ in range(MAX_BUFFER_RETRIES):
            adapter_buffer = ctypes.create_string_buffer(buffer_size.value)
            first_adapter = ctypes.cast(
                adapter_buffer,
                ctypes.POINTER(_IP_ADAPTER_ADDRESSES),
            )
            result = get_adapters_addresses(
                socket.AF_INET,
                flags,
                None,
                first_adapter,
                ctypes.byref(buffer_size),
            )
            if result != ERROR_BUFFER_OVERFLOW:
                break
        else:
            raise OSError("GetAdaptersAddresses: размер буфера постоянно меняется")

        if result == ERROR_NO_DATA:
            return ["localhost"]

        if result != 0:
            raise ctypes.WinError(result)

        regular_ips: list[str] = []
        loopback_ips: list[str] = []
        seen: set[str] = set()

        adapter = first_adapter
        while adapter:
            unicast = adapter.contents.FirstUnicastAddress
            while unicast:
                address = unicast.contents.Address
                if (
                    address.lpSockaddr
                    and address.iSockaddrLength >= ctypes.sizeof(_SOCKADDR_IN)
                ):
                    sockaddr = ctypes.cast(
                        address.lpSockaddr,
                        ctypes.POINTER(_SOCKADDR_IN),
                    ).contents
                    if sockaddr.sin_family == socket.AF_INET:
                        ip = socket.inet_ntoa(bytes(sockaddr.sin_addr))
                        address_value = ipaddress.ip_address(ip)
                        if not address_value.is_unspecified and ip not in seen:
                            seen.add(ip)
                            if address_value.is_loopback:
                                loopback_ips.append(ip)
                            else:
                                regular_ips.append(ip)
                unicast = unicast.contents.Next
            adapter = adapter.contents.Next

        loopback_ips.append("localhost")

        return regular_ips + loopback_ips

    except OSError as error:
        print(f"tf2_detect: GetAdaptersAddresses failed: {error}", file=sys.stderr)
        return _fallback_local_ips()


def _fallback_local_ips() -> list[str]:
    """Резервный способ для другой ОС или ошибки Windows API."""
    ips: list[str] = []
    seen: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if not ipaddress.ip_address(ip).is_unspecified and ip not in seen:
                seen.add(ip)
                ips.append(ip)
    except OSError as error:
        print(f"tf2_detect: getaddrinfo failed: {error}", file=sys.stderr)
    ips.append("localhost")
    return ips
