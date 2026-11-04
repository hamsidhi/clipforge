"""Use ClipForge from your phone, anywhere, privately (Phase 14).

Tailscale (free personal plan) puts the laptop and the phone on a private network ("tailnet"). `tailscale serve`
publishes http://127.0.0.1:8000 as https://<laptop>.<tailnet>.ts.net with a real certificate, visible ONLY to devices
in your tailnet. Nothing is exposed to the public internet (Funnel is never used).
CLI syntax checked 2026-11-04 (tailscale.com/kb/1242): `tailscale serve --bg --https=443 127.0.0.1:8000`,
`tailscale serve --https=443 127.0.0.1:8000 off`, `tailscale serve status --json`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ADMIN_DNS = "https://login.tailscale.com/admin/dns"
DOWNLOAD = "https://tailscale.com/download"


@dataclass
class RemoteStatus:
    installed: bool = False
    running: bool = False          # logged in + connected
    dns_name: str = ""             # laptop.tailnet.ts.net
    magic_dns: bool = False
    https_certs: bool = False
    serving: bool = False
    url: str = ""
    problems: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


def tailscale_bin() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "win32":
        for p in (Path(r"C:\Program Files\Tailscale\tailscale.exe"), Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe")):
            if p.exists():
                return str(p)
    return None


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    exe = tailscale_bin()
    if not exe:
        raise FileNotFoundError("tailscale")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def parse_status(data: dict) -> RemoteStatus:
    """Pure: interpret `tailscale status --json` (unit-tested)."""
    st = RemoteStatus(installed=True)
    st.running = data.get("BackendState") == "Running"
    me = data.get("Self") or {}
    st.dns_name = (me.get("DNSName") or "").rstrip(".")
    tn = data.get("CurrentTailnet") or {}
    st.magic_dns = bool(tn.get("MagicDNSEnabled")) or bool(data.get("MagicDNSSuffix"))
    st.https_certs = bool(data.get("CertDomains") or me.get("CertDomains"))
    if not st.running:
        st.problems.append("Tailscale isn't logged in / connected on this laptop.")
        st.steps.append("Open Tailscale from the Start menu (system tray icon) and log in.")
    if not st.magic_dns:
        st.problems.append("MagicDNS is off.")
        st.steps.append(f"Turn on MagicDNS: {ADMIN_DNS} → MagicDNS → Enable.")
    if not st.https_certs:
        st.problems.append("HTTPS certificates are off.")
        st.steps.append(f"Turn on HTTPS: {ADMIN_DNS} → HTTPS Certificates → Enable.")
    if st.dns_name:
        st.url = f"https://{st.dns_name}"
    return st


def status(port: int = 8000) -> RemoteStatus:
    if not tailscale_bin():
        return RemoteStatus(installed=False, problems=["Tailscale isn't installed on this laptop."],
                            steps=[f"Install Tailscale (free): {DOWNLOAD} — then log in with the same account as on your phone."])
    try:
        res = _run(["status", "--json"])
        st = parse_status(json.loads(res.stdout or "{}"))
    except (subprocess.SubprocessError, ValueError) as exc:
        return RemoteStatus(installed=True, problems=[f"`tailscale status` failed: {exc}"])
    try:
        sv = _run(["serve", "status", "--json"])
        st.serving = f"127.0.0.1:{port}" in (sv.stdout or "") or f"localhost:{port}" in (sv.stdout or "")
    except subprocess.SubprocessError:
        pass
    return st


def serve_command(port: int = 8000, off: bool = False) -> list[str]:
    args = ["serve", "--https=443", f"127.0.0.1:{port}"]
    return args + ["off"] if off else ["serve", "--bg", "--https=443", f"127.0.0.1:{port}"]


def setup(port: int = 8000) -> RemoteStatus:
    st = status(port)
    if not st.installed or not st.running or not st.https_certs:
        return st
    res = _run(serve_command(port), timeout=60)
    if res.returncode != 0:
        st.problems.append(f"`tailscale serve` failed: {(res.stderr or res.stdout).strip()}")
        if "access" in (res.stderr or "").lower() or "denied" in (res.stderr or "").lower():
            st.steps.append("Run it once from an Administrator PowerShell, or `tailscale set --operator=$env:USERNAME` (Linux/macOS: sudo).")
        return st
    return status(port)


def stop(port: int = 8000) -> str:
    res = _run(serve_command(port, off=True))
    return (res.stdout or res.stderr).strip() or "stopped"


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


def qr_terminal(text: str) -> str:
    import qrcode

    q = qrcode.QRCode(border=1)
    q.add_data(text)
    q.make(fit=True)
    m = q.get_matrix()
    lines = []
    for y in range(0, len(m), 2):  # two rows per character with half blocks
        top, bot = m[y], m[y + 1] if y + 1 < len(m) else [False] * len(m[y])
        lines.append("".join("█" if a and b else "▀" if a else "▄" if b else " " for a, b in zip(top, bot)))
    return "\n".join(lines)
