"""Network helpers for the VPN check and the UDP drop counter. Read-only and needs no sudo.

Linux reads /proc/net/route and /proc/net/udp; macOS uses route. WSL asks Windows (PowerShell, read-only)
which adapter it would route through. Anything that cannot be read is reported as None, never as a guess.

Standard library only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time


def _run(cmd, timeout=3):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def default_interface():
    """Name of the interface that carries the default route, or None."""
    if sys.platform == "darwin":
        m = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", "default"]))
        return m.group(1) if m else None
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return None
    best = None
    for line in lines:
        cols = line.split()
        # destination 00000000 is the default route; the lowest metric wins
        if len(cols) >= 7 and cols[1] == "00000000":
            metric = int(cols[6]) if cols[6].isdigit() else 0
            if best is None or metric < best[0]:
                best = (metric, cols[0])
    return best[1] if best else None


def is_wsl():
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _windows_powershell():
    for p in ("powershell.exe", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"):
        if shutil.which(p) or os.path.exists(p):
            return p
    return None


def windows_route(dest=None):
    """Under WSL: the Windows adapter that Windows would use to reach dest (default: the internet).
    Returns (adapter name, adapter description), or None. Reading the route needs no admin rights."""
    if not is_wsl():
        return None
    ps = _windows_powershell()
    if not ps:
        return None
    cmd = (f"Find-NetRoute -RemoteIPAddress {dest or '1.1.1.1'} | Select-Object -First 1 "
           f"InterfaceAlias,InterfaceDescription | ConvertTo-Json -Compress")
    out = _run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd], timeout=15).strip()
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("InterfaceAlias") or "", data.get("InterfaceDescription") or ""


# Windows adapters that belong to VPNs and secure-access clients (Cato included)
_WIN_VPN = re.compile(r"cato|vpn|anyconnect|globalprotect|forticlient|fortinet|wireguard|openvpn|tap-windows|"
                      r"tunnel|nordlynx|proton|pulse|zscaler|netskope|tailscale", re.I)


def is_vpn_adapter(name, description=""):
    return bool(_WIN_VPN.search(f"{name} {description}"))


def udp_drops(port):
    """Packets the kernel discarded on sockets bound to this UDP port (their receive buffer was full).
    Linux only; reads /proc/net/udp and needs no sudo. None where the counter is not available."""
    if sys.platform == "darwin":
        return None
    total, found = 0, False
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 13:
                continue
            try:
                local_port = int(cols[1].rsplit(":", 1)[1], 16)
                drops = int(cols[-1])
            except (ValueError, IndexError):
                continue
            if local_port == port:
                total += drops
                found = True
    return total if found else None


# interfaces that VPNs and tunnels create: tun/tap (OpenVPN and others), wg (WireGuard), tailscale,
# ppp, nordlynx and proton (NordVPN, Proton), ipsec and gpd (IPsec and GlobalProtect), utun (macOS tunnels)
_VPN_NAME = re.compile(r"^(tun|tap|wg|tailscale|ppp|nordlynx|proton|ipsec|gpd|utun|vpn|openvpn)", re.I)


_WIFI_NAME = re.compile(r"wi-?fi|wireless|802\.11|wlan", re.I)


def _mac_wifi_device():
    """macOS: the device name behind the 'Wi-Fi' hardware port, such as en0, or None."""
    out = _run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: Wi-Fi\s*\nDevice: (\S+)", out)
    return m.group(1) if m else None


def is_wifi(interface, description=""):
    """Is this interface a Wi-Fi link? Linux: a wireless interface in sysfs. Windows (WSL): the adapter's description."""
    if description and _WIFI_NAME.search(description):
        return True
    if not interface:
        return False
    return os.path.isdir(f"/sys/class/net/{interface}/wireless") or os.path.isdir(f"/sys/class/net/{interface}/phy80211")


def _windows_wifi_connected(netsh_output=None):
    """Under WSL: does Windows report a connected wireless adapter? Asks netsh, which needs no admin rights.
    netsh_output is for testing; normally it is read from the Windows side."""
    out = netsh_output
    if out is None:
        exe = "/mnt/c/Windows/System32/netsh.exe"
        if not os.path.exists(exe):
            return False
        try:
            res = subprocess.run([exe, "wlan", "show", "interfaces"], capture_output=True, timeout=10)
            out = res.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return False
    return bool(re.search(r"^\s*State\s*:\s*connected\s*$", out, re.M | re.I))


def network_is_wifi(peer=None):
    """True when the route the feed takes is Wi-Fi, False when it is not, None when it cannot tell.
    peer is the remote IP for a caller; None means the default route."""
    try:
        if is_wsl():
            win = windows_route(peer)
            if win and is_wifi(win[0], win[1]):
                return True
            if _windows_wifi_connected():
                return True          # Windows has a connected wireless adapter: treat this PC as on Wi-Fi
            if win:
                return False
        via = route_interface(peer) if peer else default_interface()
        if not via:
            return None
        if is_wifi(via):
            return True
        if sys.platform == "darwin":
            return via == _mac_wifi_device()
        return False
    except Exception:
        return None


def is_vpn_interface(name):
    return bool(name) and bool(_VPN_NAME.match(name))


def route_interface(dest):
    """The interface the OS would use to reach dest (an IP address), or None. Only a route lookup."""
    if not dest:
        return None
    if sys.platform == "darwin":
        m = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", dest]))
        return m.group(1) if m else None
    if not shutil.which("ip"):
        return default_interface()          # no ip command: the default route is the best guess
    m = re.search(r"\bdev (\S+)", _run(["ip", "-4", "route", "get", dest]))
    return m.group(1) if m else None
