#!/usr/bin/env python3
"""
Arsenal — Installer and manager for the pr0t31n toolkit.

Checks, installs, updates, and uninstalls all pr0t31n tools.
Requires root / sudo.

Usage:
    sudo python3 arsenal.py
    sudo arsenal          # if symlinked to /usr/local/bin
"""
from __future__ import annotations

import curses
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
import ast
import datetime
import importlib.util
import tempfile
import itertools
import threading
import time
import shlex
import atexit
import base64
import getpass

VERSION      = "1.8.0"
INSTALL_BASE = Path("/opt/pr0t31n")
SYSTEM       = platform.system()   # 'Linux' | 'Darwin'


def _install_dir(app: dict) -> Path:
    """Return the local install directory for an app.
    Uses app["install_path"] if present, otherwise INSTALL_BASE / app["repo"].
    """
    if "install_path" in app:
        return Path(app["install_path"])
    return INSTALL_BASE / app["repo"]


# ─────────────────────────────────────────────────────────────────────────────
# ANSI colour helpers (non-curses sections)
# ─────────────────────────────────────────────────────────────────────────────
_R   = "\033[0m"
_G   = "\033[32m"
_Y   = "\033[33m"
_RD  = "\033[31m"
_C   = "\033[36m"
_BLD = "\033[1m"
_DIM = "\033[2m"


# ─────────────────────────────────────────────────────────────────────────────
# GitHub credential cache (session-scoped — prompted once, reused everywhere)
# ─────────────────────────────────────────────────────────────────────────────
_GH_CREDS: dict        = {"user": "", "token": ""}
_ASKPASS_PATH: list    = [None]   # mutable box so atexit can see it


def _read_token_from_tty(prompt: str) -> str:
    """Read a secret directly from /dev/tty with echo disabled.
    More reliable than getpass under sudo on macOS/Linux."""
    try:
        import termios, tty as _tty
        with open("/dev/tty", "r+", buffering=1) as ttyf:
            ttyf.write(prompt)
            ttyf.flush()
            old_attrs = termios.tcgetattr(ttyf.fileno())
            try:
                _tty.setraw(ttyf.fileno())
                chars: list = []
                while True:
                    ch = os.read(ttyf.fileno(), 1).decode("utf-8", errors="replace")
                    if ch in ("\n", "\r"):
                        break
                    if ch in ("\x7f", "\x08"):   # backspace / DEL
                        if chars:
                            chars.pop()
                    elif ch == "\x03":            # Ctrl-C
                        raise KeyboardInterrupt
                    elif ch == "\x15":            # Ctrl-U — clear line
                        chars.clear()
                    else:
                        chars.append(ch)
                ttyf.write("\n")
                ttyf.flush()
                return "".join(chars).strip()
            finally:
                termios.tcsetattr(ttyf.fileno(), termios.TCSADRAIN, old_attrs)
    except (ImportError, OSError):
        # Non-POSIX or no /dev/tty — fall back to getpass
        return getpass.getpass(prompt).strip()


def _ensure_gh_creds() -> None:
    """Prompt for GitHub credentials once; no-op if already cached."""
    if _GH_CREDS["token"]:
        return
    print(f"\n{_BLD}GitHub credentials{_R}  "
          "(for private repos — press Enter to skip)")
    try:
        user  = input("  Username: ").strip()
        token = _read_token_from_tty("  Personal Access Token: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return
    # Sanity-check: GitHub PATs are 40+ chars (classic) or 93+ (fine-grained).
    # If we got something suspiciously short, getpass may have read from a broken
    # stdin under sudo — fall back to a visible input with an explicit warning.
    if token and len(token) < 20:
        warn(f"Token appears truncated ({len(token)} chars) — re-enter with visible input:")
        try:
            token = input("  Personal Access Token (visible): ").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
    _GH_CREDS["user"]  = user
    _GH_CREDS["token"] = token
    # Invalidate any existing askpass script so it gets rebuilt with new creds
    _ASKPASS_PATH[0] = None


def _create_askpass() -> Optional[str]:
    """Write a GIT_ASKPASS helper to /tmp (once) and return its path."""
    token = _GH_CREDS.get("token", "")
    if not token:
        return None
    if _ASKPASS_PATH[0] and Path(_ASKPASS_PATH[0]).exists():
        return _ASKPASS_PATH[0]
    try:
        fd, path = tempfile.mkstemp(suffix=".sh", prefix="arsenal_ask_")
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\n")
            f.write("case \"$1\" in\n")
            f.write(f"  Username*) echo {shlex.quote(_GH_CREDS['user'])} ;;\n")
            f.write(f"  Password*) echo {shlex.quote(token)} ;;\n")
            f.write("  *)         echo \"\" ;;\n")
            f.write("esac\n")
        os.chmod(path, 0o700)
        _ASKPASS_PATH[0] = path
        return path
    except Exception:
        return None


def _cleanup_askpass():
    p = _ASKPASS_PATH[0]
    if p:
        try:
            os.unlink(p)
        except Exception:
            pass

atexit.register(_cleanup_askpass)


def _git_env() -> dict:
    """Return os.environ copy with GIT_ASKPASS injected if credentials exist."""
    env = os.environ.copy()
    askpass = _create_askpass()
    if askpass:
        env["GIT_ASKPASS"]         = askpass
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


_GIT_NO_CWD = "Unable to read current working directory"


def _git_run(cmd: list, **kwargs) -> "subprocess.CompletedProcess":
    """Run a git command, retrying from /opt if the CWD no longer exists."""
    import subprocess as _sp
    r = _sp.run(cmd, **kwargs)
    if r.returncode != 0:
        raw = r.stderr or ""
        stderr = raw if isinstance(raw, str) else raw.decode(errors="replace")
        if _GIT_NO_CWD in stderr:
            warn("git: CWD missing — retrying from /opt …")
            kwargs["cwd"] = "/opt"
            r = _sp.run(cmd, **kwargs)
    return r


def _git_spin_run(label: str, cmd: list, **kwargs) -> "subprocess.CompletedProcess":
    """Spinner-wrapped git run with CWD-missing retry."""
    r = _spin_run(label, cmd, **kwargs)
    if r.returncode != 0:
        raw = r.stderr or ""
        stderr = raw if isinstance(raw, str) else raw.decode(errors="replace")
        if _GIT_NO_CWD in stderr:
            warn("git: CWD missing — retrying from /opt …")
            kwargs["cwd"] = "/opt"
            r = _spin_run(label, cmd, **kwargs)
    return r


def _authed_url(url: str) -> str:
    """Embed GitHub token into a clone URL if credentials are cached."""
    token = _GH_CREDS.get("token", "")
    if not token or "github.com" not in url:
        return url
    from urllib.parse import quote as _quote
    safe_token = _quote(token, safe="")
    return url.replace("https://", f"https://{safe_token}@", 1)


def _gh_api_headers() -> dict:
    """Return GitHub API headers, with Bearer auth if a token is cached."""
    hdrs = {"User-Agent": f"Arsenal/{VERSION}",
            "Accept":     "application/vnd.github+json"}
    token = _GH_CREDS.get("token", "")
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    return hdrs


def ok(m:   str) -> None: print(f"{_G}[+]{_R} {m}", flush=True)
def info(m: str) -> None: print(f"{_C}[*]{_R} {m}", flush=True)
def warn(m: str) -> None: print(f"{_Y}[~]{_R} {m}", flush=True)
def err(m:  str) -> None: print(f"{_RD}[!]{_R} {m}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# ASCII banner
# ─────────────────────────────────────────────────────────────────────────────
BANNER = r"""
 █████╗ ██████╗ ███████╗███████╗███╗   ██╗ █████╗ ██╗
██╔══██╗██╔══██╗██╔════╝██╔════╝████╗  ██║██╔══██╗██║
███████║██████╔╝███████╗█████╗  ██╔██╗ ██║███████║██║
██╔══██║██╔══██╗╚════██║██╔══╝  ██║╚██╗██║██╔══██║██║
██║  ██║██║  ██║███████║███████╗██║ ╚████║██║  ██║███████╗
╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝╚══════╝
"""

# ─────────────────────────────────────────────────────────────────────────────
# Application catalogue
# ─────────────────────────────────────────────────────────────────────────────
APPS: list[dict] = [
    {
        "name":        "Quiver",
        "repo":        "Quiver",
        "github_url":  "https://github.com/pr0t31n/Quiver.git",
        "description": "Hardware hacking command centre — 45-device registry with SSH, serial, and WebSocket shells for Hak5 and offensive security hardware.",
        "short_desc":  "Hak5 Device Manager",
        "port":        5200,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-quiver",
        "requires":    [],
    },
    {
        "name":        "Aether",
        "repo":        "Aether",
        "github_url":  "https://github.com/pr0t31n/Aether.git",
        "description": "Five-phase wireless pentest orchestrator — WPA/WPA2/WPA3 handshake capture, WPA-Enterprise credential harvesting, Dragonblood, and KRACK testing.",
        "short_desc":  "WiFi",
        "port":        None,
        "flask":       False,
        "main_file":   "aether.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Aether-Z",
        "repo":        "Aether-Z",
        "github_url":  "https://github.com/pr0t31n/Aether-Z.git",
        "description": "Multi-phase ZigBee / IEEE 802.15.4 pentest orchestrator — RF survey, network key recovery via join intercept, ZLL touchlink attacks, replay testing, and firmware analysis.",
        "short_desc":  "ZigBee",
        "port":        None,
        "flask":       False,
        "main_file":   "aether_z.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Aether-B",
        "repo":        "Aether-B",
        "github_url":  "https://github.com/pr0t31n/Aether-B.git",
        "description": "Multi-phase Bluetooth BR/EDR & BLE pentest orchestrator — KNOB, BIAS, BLUFFS, BLE legacy pairing cracking, GATT enumeration, GATTacker MITM, and BlueToolkit CVE exploitation.",
        "short_desc":  "Bluetooth",
        "port":        None,
        "flask":       False,
        "main_file":   "aether_b.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Aether-U",
        "repo":        "Aether-U",
        "github_url":  "https://github.com/pr0t31n/Aether-U.git",
        "description": "Multi-phase UWB / RTLS pentest orchestrator — Ghost Peak distance-reduction, RTLS console default-credential attacks, unauthenticated MQTT exploitation, position injection, and SQLi.",
        "short_desc":  "UWB / RTLS",
        "port":        None,
        "flask":       False,
        "main_file":   "aether_u.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Spectre",
        "repo":        "Spectre",
        "github_url":  "https://github.com/pr0t31n/Spectre.git",
        "description": "Web dashboard for Aether wireless assessments — real-time phase tracking, findings streaming, and session management via browser.",
        "short_desc":  "Wireless Assessments",
        "port":        5300,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-spectre",
        "requires":    ["Aether", "Aether-Z", "Aether-B", "Aether-U"],
    },
    {
        "name":        "Gearbox",
        "repo":        "Gearbox",
        "github_url":  "https://github.com/pr0t31n/Gearbox.git",
        "description": "Hardware security assessment CLI — orchestrates nmap, Nessus, sslscan, nuclei, Responder, and 20+ tools into structured pentest workflows.",
        "short_desc":  "Hardware / IoT",
        "port":        None,
        "flask":       False,
        "main_file":   "gearbox.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Gauntlet",
        "repo":        "Gauntlet",
        "github_url":  "https://github.com/pr0t31n/Gauntlet.git",
        "description": "Web platform for Gearbox hardware assessments — multi-tool session orchestration, real-time findings streaming, and interactive assessment dashboard.",
        "short_desc":  "Hardware Assessments",
        "port":        5700,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-gauntlet",
        "requires":    ["Gearbox"],
    },
    {
        "name":        "Pivot",
        "repo":        "Pivot",
        "github_url":  "https://github.com/pr0t31n/Pivot.git",
        "description": "mTLS-authenticated SOCKS5 relay daemon — asyncio agent/client for secure network pivoting with transparent iptables proxy support.",
        "short_desc":  "mTLS SOCKS5 Relay",
        "port":        None,
        "flask":       False,
        "main_file":   "pivot/agent/main.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Vantage",
        "repo":        "Vantage",
        "github_url":  "https://github.com/pr0t31n/Vantage.git",
        "description": "Recon platform — orchestrates Scattershot3 network pentest and OSift OSINT in a unified session-managed dashboard with live findings streaming.",
        "short_desc":  "Network & OSINT Recon",
        "port":        5100,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-vantage",
        "requires":    ["Scattershot3", "OSift"],
    },
    {
        "name":        "OSift",
        "repo":        "OSift",
        "github_url":  "https://github.com/pr0t31n/OSift.git",
        "description": "Comprehensive OSINT framework — 19 recon phases: DNS, breach intelligence, GitHub secret detection, cloud asset discovery, and M365 enumeration.",
        "short_desc":  "OSINT",
        "port":        None,
        "flask":       False,
        "main_file":   "osift.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Scattershot3",
        "repo":        "Scattershot3",
        "github_url":  "https://github.com/pr0t31n/Scattershot3.git",
        "description": "Network pentest recon — 13-phase pipeline: host discovery, port scanning, TLS analysis, AD/Kerberos enumeration, and web application testing.",
        "short_desc":  "Network",
        "port":        None,
        "flask":       False,
        "main_file":   "scattershot3.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Mole",
        "repo":        "Mole",
        "github_url":  "https://github.com/pr0t31n/Mole.git",
        "description": "Go-based SOCKS5 + DNS proxy — transparent relay with iptables/pf integration for DNS interception and tunnelling through pivot points.",
        "short_desc":  "SOCKS5 / DNS Proxy",
        "port":        None,
        "flask":       False,
        "main_file":   "cmd/client/main.go",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Nexus",
        "repo":        "Nexus",
        "github_url":  "https://github.com/pr0t31n/Nexus.git",
        "description": "Central hub — discovers and monitors all pr0t31n and third-party security tool instances across every network interface in real time.",
        "short_desc":  "Tool Hub & Monitor",
        "port":        5400,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-nexus",
        "requires":    [],
    },
    {
        "name":        "Fracture",
        "repo":        "Fracture",
        "github_url":  "https://github.com/pr0t31n/Fracture.git",
        "description": "Multi-phase web application pentest orchestrator — OWASP Top 10 2025 coverage: injection, auth bypass, IDOR, SSRF, CSRF, session attacks, and sensitive file exposure.",
        "short_desc":  "WebApp",
        "port":        None,
        "flask":       False,
        "main_file":   "fracture.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Siphon",
        "repo":        "Siphon",
        "github_url":  "https://github.com/pr0t31n/Siphon.git",
        "description": "Multi-phase API pentest orchestrator — OWASP API Top 10 2023 coverage: BOLA, BFLA, mass assignment, JWT attacks, rate limit bypass, and data exposure testing.",
        "short_desc":  "API",
        "port":        None,
        "flask":       False,
        "main_file":   "siphon.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Auger",
        "repo":        "Auger",
        "github_url":  "https://github.com/pr0t31n/Auger.git",
        "description": "Read-only database security auditor — MySQL, PostgreSQL, MSSQL, Oracle, MongoDB, Redis, Cassandra, Elasticsearch and more. Maps findings to OWASP ASVS, ISO 27001, PCI DSS, HIPAA, and NIST SP 800-53.",
        "short_desc":  "Database",
        "port":        None,
        "flask":       False,
        "main_file":   "auger.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Crucible",
        "repo":        "Crucible",
        "github_url":  "https://github.com/pr0t31n/Crucible.git",
        "description": "Web app, API and database pentest platform — orchestrates Fracture, Siphon and Auger in a unified session-managed dashboard with live findings streaming.",
        "short_desc":  "WebApp · API · Database",
        "port":        5500,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-crucible",
        "requires":    ["Fracture", "Siphon", "Auger"],
    },
    {
        "name":        "AuthProbe",
        "repo":        "AuthProbe",
        "github_url":  "https://github.com/pr0t31n/AuthProbe.git",
        "description": "Detects and probes authentication methods on web apps and APIs. Identifies Basic, Digest, NTLM, Bearer, OAuth2/OIDC, form login, session cookies, API keys, and Kerberos. Validates credentials and recommends the best method for Fracture/Siphon.",
        "short_desc":  "Auth Detection",
        "port":        None,
        "flask":       False,
        "main_file":   "authprobe.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Bastion",
        "repo":        "Bastion",
        "github_url":  "https://github.com/pr0t31n/Bastion.git",
        "description": "Firewall and network policy analysis platform — uploads configs for automated audit, vulnerability identification, and compliance reporting.",
        "short_desc":  "Firewall Analysis",
        "port":        5600,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-bastion",
        "requires":    [],
    },
    {
        "name":         "Prometheus",
        "repo":         "Prometheus",
        "install_path": "/Users/pr0t31n/Prometheus",
        "github_url":   "https://github.com/pr0t31n/Prometheus.git",
        "description": "Unified pentest report engine — ingests findings from Fracture, Siphon, Scattershot3, Gearbox, Bastion, OSift, and Aether to produce correlated Excel and HTML reports.",
        "short_desc":  "Report Engine",
        "port":        None,
        "flask":       False,
        "main_file":   "prometheus.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "pAIn",
        "repo":        "pAIn",
        "github_url":  "https://github.com/pr0t31n/pAIn.git",
        "description": "AI pipeline red-team evaluation tool — tests transcription and classification AI systems for prompt injection via embedded payloads in PDF, image, and audio file metadata, content layers, and spoken audio content.",
        "short_desc":  "AI Red-Team",
        "port":        None,
        "flask":       False,
        "main_file":   "pain.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Arbiter",
        "repo":        "Arbiter",
        "github_url":  "https://github.com/pr0t31n/Arbiter.git",
        "description": "Engagement-driven pentest orchestration platform — manages clients, projects, and scope across all pr0t31n tools with unified findings, compliance mapping (12 frameworks), Nessus integration, Burp XML import, and purgatory workflow for discovered remote projects.",
        "short_desc":  "Engagement Orchestrator",
        "port":        5000,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-arbiter",
        "requires":    [],
    },
    {
        "name":        "Arsenal",
        "repo":        "Arsenal",
        "install_path": "/opt/Arsenal",
        "github_url":  "https://github.com/pr0t31n/Arsenal.git",
        "description": "Installer and manager for the pr0t31n toolkit — checks, installs, updates, and uninstalls all pr0t31n tools.",
        "short_desc":  "Toolkit Manager",
        "port":        None,
        "flask":       False,
        "main_file":   "arsenal.py",
        "service":     None,
        "requires":    [],
    },
]

APP_INDEX: dict[str, dict] = {a["name"]: a for a in APPS}


def _build_display_items() -> list[dict]:
    """
    Build the ordered display list for the curses menu.

    Layout:
      • Every Flask app at the top level, sorted alphabetically.
      • Each Flask app's required tools indented below it, sorted alphabetically.
      • All remaining standalone tools (not managed by any Flask app) at the
        top level after the Flask block, sorted alphabetically.
    """
    managed: set[str] = set()
    for a in APPS:
        if a["flask"]:
            for req in a.get("requires", []):
                managed.add(req)

    items: list[dict] = []
    for fa in sorted((a for a in APPS if a["flask"]), key=lambda a: a["name"].lower()):
        items.append({"app": fa, "indent": 0})
        for child_name in sorted(fa.get("requires", [])):
            child = APP_INDEX.get(child_name)
            if child:
                items.append({"app": child, "indent": 1})

    for sa in sorted(
        (a for a in APPS if not a["flask"] and a["name"] not in managed),
        key=lambda a: a["name"].lower(),
    ):
        items.append({"app": sa, "indent": 0})

    return items


_DISPLAY_ITEMS: list[dict] = _build_display_items()


# ─────────────────────────────────────────────────────────────────────────────
# Root check
# ─────────────────────────────────────────────────────────────────────────────
def check_root() -> None:
    if os.geteuid() != 0:
        err("Arsenal requires elevated privileges.")
        print(f"  Run:  {_RD}sudo python3 {Path(__file__).name}{_R}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Version helpers
# ─────────────────────────────────────────────────────────────────────────────
def _version_gt(v1: str, v2: str) -> bool:
    """Return True if v1 is strictly greater than v2 (semver tuple comparison)."""
    try:
        return tuple(int(x) for x in v1.split(".")) > tuple(int(x) for x in v2.split("."))
    except Exception:
        return False


def get_local_version(app: dict) -> Optional[str]:
    """Read the installed version — plain VERSION file first, then source constants."""
    install_dir = _install_dir(app)
    if not install_dir.is_dir():
        return None

    # Plain VERSION file (authoritative for all pr0t31n tools)
    version_file = install_dir / "VERSION"
    if version_file.exists():
        try:
            ver = version_file.read_text(encoding="utf-8").strip()
            if ver:
                return ver
        except Exception:
            pass

    # Fall back to reading the VERSION constant from source
    candidates = [app["main_file"], "app.py", f"{app['name'].lower()}.py", "main.py", "main.go"]
    for fname in candidates:
        f = install_dir / fname
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            # Python: VERSION = "1.0.0"  or  VERSION = '1.0.0'
            m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
            if m:
                return m.group(1)
            # Go: var version = "1.0.0"
            m = re.search(r'var\s+version\s*=\s*"([^"]+)"', text)
            if m:
                return m.group(1)
        except Exception:
            pass
    return "unknown"


def _check_github_connection() -> None:
    """Test GitHub API reachability and credential validity; print status."""
    # 1 — Network reachability
    try:
        req = urllib.request.Request(
            "https://api.github.com/rate_limit",
            headers={"User-Agent": f"Arsenal/{VERSION}",
                     "Accept":     "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            rl = json.loads(r.read())
        unauth_remaining = rl.get("rate", {}).get("remaining", 0)
    except urllib.error.URLError as e:
        err(f"GitHub unreachable: {e.reason}")
        return
    except Exception as e:
        err(f"GitHub unreachable: {type(e).__name__}: {e}")
        return

    ok("github.com reachable")

    # 2 — Credential check (only if a token was entered)
    token = _GH_CREDS.get("token", "")
    if not token:
        warn("No GitHub credentials entered — private repo versions will show '?'")
        return

    tok_len = len(token)
    auth_hdrs = {"User-Agent": f"Arsenal/{VERSION}",
                 "Accept":     "application/vnd.github+json",
                 "Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(
            "https://api.github.com/rate_limit",
            headers=auth_hdrs,
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        authed_remaining = data.get("rate", {}).get("remaining", 0)
        authed_limit     = data.get("rate", {}).get("limit", 0)
        if authed_limit > 60:
            ok(f"GitHub credentials accepted  ({authed_remaining}/{authed_limit} API calls remaining)")
        else:
            # Authenticated requests get 5000/hr; if limit is still 60 the token wasn't accepted
            warn(f"GitHub credentials may not have been accepted (rate limit unchanged — token {tok_len} chars, check token scope/expiry)")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            err(f"GitHub credentials rejected (401 Unauthorized) — token captured {tok_len} chars; may be expired, wrong scope, or truncated")
        else:
            warn(f"GitHub credential check returned HTTP {e.code} (token {tok_len} chars)")
    except Exception as e:
        warn(f"GitHub credential check failed: {type(e).__name__}: {e}")


def get_github_version(app: dict) -> Optional[str]:
    """Read the VERSION file from the GitHub repo contents API."""
    repo = app["repo"]
    hdrs = _gh_api_headers()

    # Primary: read VERSION file directly from the default branch
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/pr0t31n/{repo}/contents/VERSION",
            headers=hdrs,
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
            content = data.get("content", "")
            encoding = data.get("encoding", "base64")
            if content and encoding == "base64":
                ver = base64.b64decode(content).decode("utf-8", errors="replace").strip()
                if ver:
                    return ver
    except Exception:
        pass

    # Fallback: latest release tag
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/pr0t31n/{repo}/releases/latest",
            headers=hdrs,
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
            tag = data.get("tag_name", "")
            if tag:
                return tag.lstrip("v")
    except Exception:
        pass

    # Fallback: most recent tag
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/pr0t31n/{repo}/tags",
            headers=hdrs,
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            tags = json.loads(r.read())
            if tags:
                return tags[0]["name"].lstrip("v")
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# macOS plist management
# ─────────────────────────────────────────────────────────────────────────────
def _write_plist(app: dict) -> Optional[Path]:
    """
    Write (or overwrite) the LaunchDaemon plist for a Flask app on macOS,
    always using INSTALL_BASE / repo as the working directory.
    Returns the plist path on success, None if not macOS or not a Flask app.
    """
    if SYSTEM != "Darwin" or not app.get("flask"):
        return None

    python_bin = shutil.which("python3") or sys.executable
    install_dir = _install_dir(app)
    main_script = install_dir / app["main_file"]
    label       = f"com.pr0t31n.{app['name'].lower()}"
    plist_path  = Path(f"/Library/LaunchDaemons/{label}.plist")
    log_out     = f"/var/log/pr0t31n-{app['name'].lower()}.log"
    log_err     = f"/var/log/pr0t31n-{app['name'].lower()}.err"

    env_block = ""
    if app.get("port"):
        env_block = (
            f"    <key>EnvironmentVariables</key>\n"
            f"    <dict>\n"
            f"        <key>PORT</key>\n"
            f"        <string>{app['port']}</string>\n"
            f"    </dict>\n"
        )

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{main_script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{install_dir}</string>
{env_block}    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""
    try:
        plist_path.write_text(content)
        plist_path.chmod(0o644)
        ok(f"Wrote plist: {plist_path}  →  {main_script}")
        return plist_path
    except Exception as exc:
        warn(f"Could not write plist for {app['name']}: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Service status
# ─────────────────────────────────────────────────────────────────────────────
def get_service_status(app: dict) -> str:
    """Return the current service status string for a Flask app."""
    svc = app.get("service")
    if not svc:
        return ""
    try:
        if SYSTEM == "Linux":
            if shutil.which("systemctl"):
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=4,
                )
                return r.stdout.strip() or "unknown"
            # SysVinit / OpenRC fallback
            if shutil.which("service"):
                r = subprocess.run(
                    ["service", svc, "status"],
                    capture_output=True, text=True, timeout=4,
                )
                return "running" if r.returncode == 0 else "stopped"
        elif SYSTEM == "Darwin":
            label = f"com.pr0t31n.{app['name'].lower()}"
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=4,
            )
            return "running" if r.returncode == 0 else "stopped"
        elif SYSTEM == "Windows":
            r = subprocess.run(
                ["sc", "query", svc],
                capture_output=True, text=True, timeout=6,
            )
            return "running" if "RUNNING" in r.stdout else "stopped"
        else:
            # FreeBSD / NetBSD: service(8)
            if shutil.which("service"):
                r = subprocess.run(
                    ["service", svc, "status"],
                    capture_output=True, text=True, timeout=4,
                )
                return "running" if r.returncode == 0 else "stopped"
            # OpenBSD: rcctl(8)
            if shutil.which("rcctl"):
                r = subprocess.run(
                    ["rcctl", "check", svc],
                    capture_output=True, text=True, timeout=4,
                )
                return "running" if r.returncode == 0 else "stopped"
    except Exception:
        pass
    return "unknown"


def get_running_version(app: dict) -> str:
    """Return the running version of a Flask app.

    Resolution order:
      1. HTTP probe of /api/nexus-status (preferred — confirms the actual
         live process is responsive on the configured port).
      2. systemctl is-active <svc>: if the unit is active, the running
         process loaded the same files on disk as the installed copy, so
         the local VERSION constant is what's running. Useful when an app
         has no /api/nexus-status endpoint, when the port is bound only
         to a UNIX socket / reverse-proxy, or when TLS configuration
         breaks the probe.
      3. Empty string if neither path yields a version.
    """
    # Step 1: HTTP probe
    if app.get("flask") and app.get("port"):
        import ssl as _ssl
        port = app["port"]
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        for scheme, extra in (("https", {"context": ctx}), ("http", {})):
            try:
                url = f"{scheme}://127.0.0.1:{port}/api/nexus-status"
                req = urllib.request.Request(
                    url, headers={"User-Agent": f"Arsenal/{VERSION}"})
                with urllib.request.urlopen(req, timeout=2, **extra) as r:
                    data = json.loads(r.read())
                    v = str(data.get("version") or "").strip()
                    if v:
                        return v
            except Exception:
                pass

    # Step 2: systemctl / launchctl fallback — if the service is active,
    # the running version IS the installed version (systemd loaded the
    # same files on disk). Suffix the value with a marker so operators
    # can distinguish authoritative HTTP-probed versions from derived ones.
    svc = app.get("service")
    if not svc:
        return ""
    try:
        if SYSTEM == "Linux" and shutil.which("systemctl"):
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=4,
            )
            if r.stdout.strip() == "active":
                lv = get_local_version(app)
                if lv:
                    return f"{lv} (via systemctl)"
        elif SYSTEM == "Darwin":
            label = f"com.pr0t31n.{app['name'].lower()}"
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0:
                lv = get_local_version(app)
                if lv:
                    return f"{lv} (via launchctl)"
    except Exception:
        pass
    return ""



# ─────────────────────────────────────────────────────────────────────────────
# Restart helpers
# ─────────────────────────────────────────────────────────────────────────────
def _kill_port(port: int) -> None:
    """Kill any process currently listening on the given TCP port."""
    try:
        if SYSTEM == "Linux":
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True, timeout=5, check=False,
            )
        elif SYSTEM == "Darwin":
            r = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid in r.stdout.strip().splitlines():
                pid = pid.strip()
                if pid.isdigit():
                    subprocess.run(["kill", "-9", pid], check=False, timeout=3)
    except Exception:
        pass


def _process_restart(app: dict) -> bool:
    """Kill the process on app's port and spawn a fresh instance in the background."""
    port        = app.get("port")
    install_dir = _install_dir(app)
    app_script  = install_dir / "app.py"

    if not app_script.exists():
        err(f"{app['name']}: app.py not found at {app_script} — cannot restart")
        return False

    if port:
        _kill_port(port)

    try:
        subprocess.Popen(
            [sys.executable, str(app_script)],
            cwd=str(install_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        ok(f"{app['name']} spawned (port {port}).")
        return True
    except Exception as exc:
        err(f"Failed to spawn {app['name']}: {exc}")
        return False


def restart_app(app: dict) -> bool:
    """
    Restart a Flask web application.

    On Linux uses systemctl restart when a unit file exists; falls back to
    process-level kill + spawn when the service is not registered.
    On macOS uses launchctl stop/start when a plist exists; same fallback.
    """
    svc  = app.get("service")
    name = app["name"]
    port = app.get("port")
    info(f"Restarting {name}…")

    if SYSTEM == "Linux":
        if svc:
            svc_file = Path(f"/etc/systemd/system/{svc}.service")
            if svc_file.exists():
                r = subprocess.run(
                    ["systemctl", "restart", svc],
                    check=False, timeout=20,
                )
                if r.returncode == 0:
                    ok(f"{name} restarted via systemctl.")
                    return True
                warn(f"systemctl restart {svc} failed (code {r.returncode}) "
                     "— falling back to process restart.")
        return _process_restart(app)

    if SYSTEM == "Darwin":
        if svc:
            label      = f"com.pr0t31n.{name.lower()}"
            plist_path = Path(f"/Library/LaunchDaemons/{label}.plist")
            # Always (re)write the plist so paths stay anchored to INSTALL_BASE
            _write_plist(app)
            if plist_path.exists():
                # Full unload/load cycle — stop/start only suspends the process;
                # unload removes it from launchd so load re-reads the plist.
                subprocess.run(["launchctl", "unload", "-w", str(plist_path)],
                               check=False, timeout=10)
                r = subprocess.run(["launchctl", "load",  "-w", str(plist_path)],
                                   check=False, timeout=10)
                if r.returncode == 0:
                    ok(f"{name} restarted via launchctl (unload → load).")
                    return True
                warn(f"launchctl load {plist_path} failed (code {r.returncode}) "
                     "— falling back to process restart.")
        return _process_restart(app)

    err(f"Unsupported platform for restart: {SYSTEM}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Status loading
# ─────────────────────────────────────────────────────────────────────────────
def load_status() -> dict[str, dict]:
    """
    Return per-app status dict with keys:
        installed, local_ver, github_ver, needs_update, service_status
    """
    info("Checking installation status…")
    status: dict[str, dict] = {}
    for app in APPS:
        installed  = _install_dir(app).is_dir()
        local_ver  = get_local_version(app) if installed else None
        github_ver = get_github_version(app)
        needs_upd  = bool(
            installed
            and local_ver
            and github_ver
            and local_ver not in ("unknown", github_ver)
        )
        svc_status  = (get_service_status(app)
                       if installed and app["flask"] else "")
        running_ver = (get_running_version(app)
                       if installed and app["flask"] else "")
        status[app["name"]] = {
            "installed":      installed,
            "local_ver":      local_ver,
            "github_ver":     github_ver,
            "needs_update":   needs_upd,
            "service_status": svc_status,
            "running_ver":    running_ver,
        }
    return status


def refresh_local_status(status: dict[str, dict], with_github: bool = False) -> None:
    """
    Refresh installed/local_ver/service_status/running_ver for every app
    in-place. When *with_github* is True, also re-fetches the Latest
    (github_ver) column. Default is False so the curses status header
    on the hot path stays fast (no network round-trips); the explicit
    user-triggered Refresh action passes with_github=True so every
    visible column updates.
    """
    for app in APPS:
        name      = app["name"]
        installed = _install_dir(app).is_dir()
        status[name]["installed"] = installed
        if installed:
            lv = get_local_version(app)
            status[name]["local_ver"] = lv
            if with_github:
                status[name]["github_ver"] = get_github_version(app)
            gv = status[name].get("github_ver") or ""
            status[name]["needs_update"] = bool(
                lv and gv and lv not in ("unknown", gv)
                and _version_gt(gv, lv)
            )
            if app["flask"]:
                status[name]["service_status"] = get_service_status(app)
                status[name]["running_ver"]    = get_running_version(app)
        else:
            status[name]["local_ver"]      = None
            status[name]["service_status"] = ""
            status[name]["running_ver"]    = ""
            status[name]["needs_update"]   = False
            if with_github:
                status[name]["github_ver"] = get_github_version(app)


# ─────────────────────────────────────────────────────────────────────────────
# Status table (printed before the curses menu)
# ─────────────────────────────────────────────────────────────────────────────
def print_status_table(status: dict[str, dict]) -> None:
    W_NAME = 14
    W_VER  = 9
    W_PORT = 6
    W_SVC  = 10
    print()
    hdr = (f"  {'':3}  {'Application':<{W_NAME}}  "
           f"{'Installed':<{W_VER}}  {'Latest':<{W_VER}}  "
           f"{'Port':<{W_PORT}}  {'Service':<{W_SVC}}")
    sep = "  " + "─" * (W_NAME + W_VER * 2 + W_PORT + W_SVC + 14)
    print(f"{_BLD}{hdr}{_R}")
    print(sep)

    for app in APPS:
        st   = status[app["name"]]
        lv   = st["local_ver"]  or "—"
        gv   = st["github_ver"] or "?"
        port = str(app["port"]) if app["port"] else "—"
        svc  = st["service_status"] or "—"
        inst = st["installed"]

        mark  = f"{_G}✓{_R}" if inst else f"{_RD}✗{_R}"
        v_col = (_Y if st["needs_update"] else (_G if inst else _DIM))
        s_col = (_G if svc in ("active", "running")
                 else _RD if svc in ("failed", "inactive") else _DIM)

        print(
            f"  {mark}  {_BLD if inst else _DIM}{app['name']:<{W_NAME}}{_R}  "
            f"{v_col}{lv:<{W_VER}}{_R}  "
            f"{gv:<{W_VER}}  "
            f"{port:<{W_PORT}}  "
            f"{s_col}{svc:<{W_SVC}}{_R}"
        )
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Update check
# ─────────────────────────────────────────────────────────────────────────────
def check_updates(status: dict[str, dict]) -> None:
    """Offer to update any installed apps that are behind the latest GitHub version."""
    outdated = [a for a in APPS if status[a["name"]]["needs_update"]]
    if not outdated:
        return

    print(f"{_Y}Updates available:{_R}")
    for app in outdated:
        st = status[app["name"]]
        print(f"  {_Y}▲{_R}  {app['name']:<15}  "
              f"{st['local_ver']}  →  {_G}{st['github_ver']}{_R}")
    print()
    try:
        ans = input(f"{_BLD}Update all now? [y/N]{_R} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans == "y":
        for app in outdated:
            _update_app(app)
            status[app["name"]]["local_ver"]  = get_local_version(app)
            status[app["name"]]["needs_update"] = False
    print()


def _update_app(app: dict) -> None:
    install_dir = _install_dir(app)
    info(f"Updating {app['name']}…")
    r = _git_run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                 capture_output=True, text=True, env=_git_env())
    if r.returncode == 0:
        _run_install_py(install_dir)
        ok(f"{app['name']} updated.")
    else:
        err(f"git pull failed for {app['name']}: {r.stderr.strip()[:120]}")


# ─────────────────────────────────────────────────────────────────────────────
# Install / Uninstall
# ─────────────────────────────────────────────────────────────────────────────
def _run_install_py(install_dir: Path) -> None:
    installer = install_dir / "install.py"
    if installer.exists():
        info(f"Running install.py in {install_dir.name}…")
        subprocess.run([sys.executable, str(installer)], cwd=str(install_dir))
    else:
        warn(f"No install.py found in {install_dir.name} — skipping post-install.")


def install_app(app: dict) -> None:
    """Clone repo into INSTALL_BASE (or app install_path) and run install.py."""
    _ensure_gh_creds()
    install_dir = _install_dir(app)
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    git_env     = _git_env()

    if install_dir.is_dir():
        info(f"{app['name']} already present — pulling latest…")
        _git_run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                 capture_output=True, text=True, env=git_env)
    else:
        info(f"Cloning {app['github_url']}  →  {install_dir}")
        r = _git_run([
            "git", "clone", "--depth=1", _authed_url(app["github_url"]), str(install_dir),
        ], capture_output=True, text=True, env=git_env)
        if r.returncode != 0:
            err(f"Clone failed for {app['name']}.")
            return

    _run_install_py(install_dir)
    _write_plist(app)
    ok(f"{app['name']} installed.")


def uninstall_app(app: dict) -> None:
    """Stop/disable service (if any), remove symlinks, delete install directory."""
    install_dir = _install_dir(app)

    # ── Service removal ──────────────────────────────────────────────────────
    svc = app.get("service")
    if svc:
        if SYSTEM == "Linux":
            subprocess.run(["systemctl", "stop",    svc], check=False)
            subprocess.run(["systemctl", "disable", svc], check=False)
            svc_file = Path(f"/etc/systemd/system/{svc}.service")
            if svc_file.exists():
                svc_file.unlink()
            subprocess.run(["systemctl", "daemon-reload"], check=False)
        elif SYSTEM == "Darwin":
            label      = f"com.pr0t31n.{app['name'].lower()}"
            plist_path = Path(f"/Library/LaunchDaemons/{label}.plist")
            if plist_path.exists():
                subprocess.run(["launchctl", "unload", "-w", str(plist_path)], check=False)
                plist_path.unlink()

    # ── Symlink cleanup ───────────────────────────────────────────────────────
    bin_dir    = Path("/usr/local/bin")
    link_names = {
        app["name"].lower(),
        app["main_file"].split("/")[-1].replace(".py", ""),
    }
    for ln in link_names:
        lk = bin_dir / ln
        if lk.is_symlink() and str(lk.resolve()).startswith(str(_install_dir(app))):
            lk.unlink()
            info(f"Removed symlink: {lk}")

    # ── Directory removal ─────────────────────────────────────────────────────
    if install_dir.is_dir():
        shutil.rmtree(install_dir)
        ok(f"{app['name']} uninstalled.")
    else:
        warn(f"{app['name']} not found at {install_dir}.")


# ─────────────────────────────────────────────────────────────────────────────
# Dependency resolution
# ─────────────────────────────────────────────────────────────────────────────
def resolve_dependencies(
    to_install:   list[str],
    to_uninstall: list[str],
    status:       dict[str, dict],
) -> tuple[list[str], list[str]]:
    """
    Expand to_install with any missing required dependencies.
    Warn if uninstalling something that another installed app depends on.
    Returns (ordered_install_list, uninstall_list).
    """
    install_set   = set(to_install)
    uninstall_set = set(to_uninstall)
    added: list[str] = []

    for name in list(to_install):
        for dep in APP_INDEX[name].get("requires", []):
            if dep not in install_set and not status.get(dep, {}).get("installed"):
                install_set.add(dep)
                added.append(dep)

    if added:
        print()
        warn("Auto-adding required dependencies:")
        for a in added:
            print(f"    {_Y}+{_R}  {a}")

    for name in list(to_uninstall):
        for app in APPS:
            if (name in app.get("requires", [])
                    and status.get(app["name"], {}).get("installed")
                    and app["name"] not in uninstall_set):
                warn(f"Removing {name} will break {app['name']} (which requires it).")

    # Topological install order (deps before dependents)
    ordered: list[str] = []
    visited: set[str]  = set()

    def _add(n: str) -> None:
        if n in visited:
            return
        visited.add(n)
        for dep in APP_INDEX[n].get("requires", []):
            if dep in install_set:
                _add(dep)
        ordered.append(n)

    for n in install_set:
        _add(n)

    return ordered, list(uninstall_set)


# ─────────────────────────────────────────────────────────────────────────────
# curses colour pairs
# ─────────────────────────────────────────────────────────────────────────────
_CP_HEADER       = 1   # cyan
_CP_NORMAL       = 2   # white
_CP_HILITE       = 3   # black on yellow (selected row)
_CP_CHECKED      = 4   # green  (checked / installed dot)
_CP_UNCKD        = 5   # dim    (unchecked / not-installed dot)
_CP_TO_UNINST    = 6   # red    (Uninstall button)
_CP_TO_INST      = 7   # green  (unused marker, kept for compat)
_CP_EXEC_BTN     = 8   # bold green  (Install button)
_CP_QUIT_BTN     = 9   # bold red    (Quit button)
_CP_REFRESH_BTN  = 10  # bold cyan   (Restart button)
_CP_UPGRADE_BTN  = 11  # bold yellow (Upgrade / Push buttons)
_CP_VER_OLD      = 12  # red no-bold (stale version display — dark red)


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_CP_HEADER,     curses.COLOR_CYAN,   -1)
    curses.init_pair(_CP_NORMAL,     curses.COLOR_WHITE,  -1)
    curses.init_pair(_CP_HILITE,     curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(_CP_CHECKED,    curses.COLOR_GREEN,  -1)
    curses.init_pair(_CP_UNCKD,      curses.COLOR_WHITE,  -1)
    curses.init_pair(_CP_TO_UNINST,  curses.COLOR_RED,    -1)
    curses.init_pair(_CP_TO_INST,    curses.COLOR_GREEN,  -1)
    curses.init_pair(_CP_EXEC_BTN,   curses.COLOR_GREEN,  -1)
    curses.init_pair(_CP_QUIT_BTN,   curses.COLOR_RED,    -1)
    curses.init_pair(_CP_REFRESH_BTN, curses.COLOR_CYAN,  -1)
    curses.init_pair(_CP_UPGRADE_BTN, curses.COLOR_YELLOW,-1)
    curses.init_pair(_CP_VER_OLD,    curses.COLOR_RED,    -1)


# ─────────────────────────────────────────────────────────────────────────────
# Interactive curses selection menu
# ─────────────────────────────────────────────────────────────────────────────
def _curses_menu(
    stdscr: "curses._CursesWindow",
    status: dict[str, dict],
) -> Optional[dict]:
    """
    Interactive checkbox list.  Checkboxes select tools; install state is
    shown separately via the ●/○ indicator.  Buttons light up based on what
    is checked and the current install / version state of those tools.

    Keys:
        ↑ / k          — move up
        ↓ / j          — move down
        SPACE / ENTER  — toggle checkbox / activate button
        i / e          — install checked (not-yet-installed) tools
        x              — uninstall checked (installed) tools
        U              — upgrade checked tools whose GitHub ver is newer
        p              — push checked tools whose local ver is newer
        r              — restart Flask app under cursor
        R              — restart all checked installed Flask apps
        f              — refresh local versions and service status
        q              — quit without changes
    """
    curses.curs_set(0)
    _init_colors()

    N = len(_DISPLAY_ITEMS)

    # Checkboxes start unchecked — install state is displayed separately
    desired: dict[str, bool] = {a["name"]: False for a in APPS}

    # Navigation: rows 0..N-1 = apps, then action buttons
    ROW_INSTALL        = N
    ROW_UNINSTALL      = N + 1
    ROW_UPGRADE        = N + 2
    ROW_PUSH           = N + 3
    ROW_RESTART        = N + 4
    ROW_REFRESH_STATUS = N + 5
    ROW_QUIT           = N + 6
    TOTAL              = N + 7
    cursor = 0
    scroll = 0  # index of first visible app row in viewport

    # Fixed column offsets for the row format:
    # "  [x]  ●  name<18  lv<9  gv<9  rv<9  port<6  desc"
    #  01234567890123456789012345678901234567890123456789012
    #  0         1         2         3         4         5
    COL_CHK  = 2
    COL_INST = 7
    COL_LV   = 30
    COL_GV   = 41
    COL_RV   = 52

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        safe_x = max_x - 1

        if max_y < 12 or max_x < 60:
            stdscr.addstr(0, 0, "Terminal too small — resize and retry.")
            stdscr.refresh()
            stdscr.getch()
            return None

        # ── column header ────────────────────────────────────────────────────
        hdr1 = f"  Arsenal v{VERSION} — Tool Selection"
        stdscr.addstr(0, 0, hdr1[:safe_x],
                      curses.color_pair(_CP_HEADER) | curses.A_BOLD)

        col_hdr = (
            f"  {'SEL':3}  {'':1}  {'Application':<18}  "
            f"{'Inst.Ver':<9}  {'Latest':<9}  {'Running':<9}  {'Port':<6}  Description"
        )
        stdscr.addstr(1, 0, col_hdr[:safe_x], curses.color_pair(_CP_HEADER))

        sep_line = "  " + "─" * min(max_x - 4, 100)
        stdscr.addstr(2, 0, sep_line[:safe_x], curses.color_pair(_CP_HEADER))

        # ── visible app rows ─────────────────────────────────────────────────
        # 3 header rows + 4 footer rows
        viewport = max_y - 7
        viewport = max(1, viewport)

        # Adjust scroll so cursor stays visible (for app rows only)
        if cursor < N:
            if cursor < scroll:
                scroll = cursor
            elif cursor >= scroll + viewport:
                scroll = cursor - viewport + 1
        scroll = max(0, min(scroll, max(0, N - viewport)))

        for vi in range(viewport):
            row_idx  = vi + scroll
            screen_y = vi + 3
            if screen_y >= max_y - 4:
                break
            if row_idx >= N:
                break

            _di      = _DISPLAY_ITEMS[row_idx]
            app      = _di["app"]
            _indent  = _di["indent"]
            name     = app["name"]
            st       = status[name]
            d        = desired[name]
            orig     = st["installed"]

            # Checkbox: checked/unchecked only — install state shown separately
            if d:
                chk, chk_cp = "[✓]", _CP_CHECKED
            else:
                chk, chk_cp = "[ ]", _CP_UNCKD

            # Install status indicator (●=installed, ○=not installed)
            if orig:
                inst_marker, inst_cp = "●", _CP_CHECKED
            else:
                inst_marker, inst_cp = "○", _CP_UNCKD

            lv_raw = st["local_ver"]   or ""
            gv_raw = st["github_ver"]  or ""
            rv_raw = st.get("running_ver", "") or ""
            lv     = lv_raw or "—"
            gv     = gv_raw or "?"
            rv     = rv_raw or ("—" if app["flask"] else "")
            port   = str(app["port"]) if app["port"] else "—"

            name_col = ("  ↳ " + name) if _indent else name
            desc_w   = max(0, safe_x - 73)
            desc     = app.get("short_desc", app["description"])[:desc_w] if desc_w > 0 else ""

            row_text = (
                f"  {chk}  {inst_marker}  {name_col:<18}  {lv:<9}  {gv:<9}  {rv:<9}  {port:<6}  {desc}"
            )

            is_on = (row_idx == cursor)
            row_attr = (curses.color_pair(_CP_HILITE)
                        if is_on else curses.color_pair(_CP_NORMAL))
            try:
                stdscr.addstr(screen_y, 0, row_text[:safe_x], row_attr)

                # Re-paint checkbox with semantic colour
                chk_attr = (curses.color_pair(_CP_HILITE) | curses.A_BOLD
                             if is_on
                             else curses.color_pair(chk_cp) | curses.A_BOLD)
                stdscr.addstr(screen_y, COL_CHK, chk, chk_attr)

                # Re-paint install status dot
                inst_attr = (curses.color_pair(_CP_HILITE) | curses.A_BOLD
                              if is_on
                              else curses.color_pair(inst_cp) | curses.A_BOLD)
                stdscr.addstr(screen_y, COL_INST, inst_marker, inst_attr)

                # Re-paint version columns when they differ (skip hilite row)
                if (not is_on and lv_raw and gv_raw
                        and lv_raw not in ("unknown",) and gv_raw not in ("?",)):
                    if _version_gt(gv_raw, lv_raw) and COL_LV < safe_x:
                        # GitHub newer → installed version is stale → dark red
                        stdscr.addstr(screen_y, COL_LV,
                                      f"{lv:<9}"[:safe_x - COL_LV],
                                      curses.color_pair(_CP_VER_OLD))
                    elif _version_gt(lv_raw, gv_raw) and COL_GV < safe_x:
                        # Local newer → latest column is behind → dark red
                        stdscr.addstr(screen_y, COL_GV,
                                      f"{gv:<9}"[:safe_x - COL_GV],
                                      curses.color_pair(_CP_VER_OLD))

                # Re-paint running version column (Flask apps only, skip hilite row)
                if not is_on and app["flask"] and COL_RV < safe_x:
                    if rv_raw and lv_raw and rv_raw != lv_raw:
                        # Running a different version than what's on disk → yellow
                        stdscr.addstr(screen_y, COL_RV,
                                      f"{rv:<9}"[:safe_x - COL_RV],
                                      curses.color_pair(_CP_UPGRADE_BTN))
                    elif rv_raw and COL_RV < safe_x:
                        # Running matches installed → green
                        stdscr.addstr(screen_y, COL_RV,
                                      f"{rv:<9}"[:safe_x - COL_RV],
                                      curses.color_pair(_CP_CHECKED))
            except curses.error:
                pass

        # ── scrollbar (right edge, app area only) ────────────────────────────
        if N > viewport:
            bar_h = viewport
            thumb_pos = int(scroll / max(1, N - viewport) * (bar_h - 1))
            for bi in range(bar_h):
                ch = "█" if bi == thumb_pos else "│"
                try:
                    stdscr.addch(3 + bi, safe_x, ch,
                                 curses.color_pair(_CP_HEADER))
                except curses.error:
                    pass

        # ── footer ────────────────────────────────────────────────────────────
        fy = max_y - 4
        try:
            stdscr.addstr(fy, 0, sep_line[:safe_x], curses.color_pair(_CP_HEADER))
        except curses.error:
            pass

        # Button availability — all scoped to currently checked tools
        checked_names = [a["name"] for a in APPS if desired[a["name"]]]

        can_install = any(
            not status[n]["installed"] for n in checked_names
        )
        can_uninstall = any(
            status[n]["installed"] for n in checked_names
        )
        can_upgrade = any(
            status[n]["installed"]
            and _version_gt(
                status[n].get("github_ver") or "",
                status[n].get("local_ver") or "",
            )
            for n in checked_names
        )
        can_push = any(
            status[n]["installed"]
            and _version_gt(
                status[n].get("local_ver") or "",
                status[n].get("github_ver") or "",
            )
            for n in checked_names
        )
        can_restart = any(
            item["app"]["flask"]
            and status[item["app"]["name"]]["installed"]
            and desired[item["app"]["name"]]
            for item in _DISPLAY_ITEMS
        )

        install_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_INSTALL
            else (curses.color_pair(_CP_EXEC_BTN) | curses.A_BOLD) if can_install
            else (curses.color_pair(_CP_UNCKD) | curses.A_DIM)
        )
        uninstall_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_UNINSTALL
            else (curses.color_pair(_CP_TO_UNINST) | curses.A_BOLD) if can_uninstall
            else (curses.color_pair(_CP_UNCKD) | curses.A_DIM)
        )
        upgrade_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_UPGRADE
            else (curses.color_pair(_CP_UPGRADE_BTN) | curses.A_BOLD) if can_upgrade
            else (curses.color_pair(_CP_UNCKD) | curses.A_DIM)
        )
        push_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_PUSH
            else (curses.color_pair(_CP_UPGRADE_BTN) | curses.A_BOLD) if can_push
            else (curses.color_pair(_CP_UNCKD) | curses.A_DIM)
        )
        restart_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_RESTART
            else (curses.color_pair(_CP_REFRESH_BTN) | curses.A_BOLD) if can_restart
            else (curses.color_pair(_CP_UNCKD) | curses.A_DIM)
        )
        refresh_status_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_REFRESH_STATUS
            else (curses.color_pair(_CP_REFRESH_BTN) | curses.A_BOLD)
        )
        quit_attr = (
            (curses.color_pair(_CP_HILITE) | curses.A_BOLD) if cursor == ROW_QUIT
            else (curses.color_pair(_CP_QUIT_BTN) | curses.A_BOLD)
        )

        try:
            stdscr.addstr(fy + 1, 2,  "[ Install ]",   install_attr)
            stdscr.addstr(fy + 1, 15, "[ Uninstall ]", uninstall_attr)
            stdscr.addstr(fy + 1, 30, "[ Upgrade ]",   upgrade_attr)
            stdscr.addstr(fy + 1, 43, "[ Push ]",      push_attr)
            stdscr.addstr(fy + 1, 53, "[ Restart ]",   restart_attr)
            stdscr.addstr(fy + 1, 66, "[ Refresh ]",   refresh_status_attr)
            stdscr.addstr(fy + 1, 79, "[ Quit ]",      quit_attr)
        except curses.error:
            pass

        try:
            hint = ("  ↑/↓: navigate   SPC: select   "
                    "i: install   x: uninstall   U: upgrade   p: push   r: restart   f: refresh   q: quit")
            stdscr.addstr(fy + 2, 0, hint[:safe_x], curses.color_pair(_CP_HEADER))
        except curses.error:
            pass

        try:
            req = ("  ●=installed  ○=not installed   "
                   "Dependencies: Spectre→Aether  Gauntlet→Gearbox  Vantage→Scattershot3+OSift")
            stdscr.addstr(fy + 3, 0, req[:safe_x],
                          curses.color_pair(_CP_UNCKD) | curses.A_DIM)
        except curses.error:
            pass

        stdscr.refresh()

        # ── keyboard input ────────────────────────────────────────────────────
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % TOTAL

        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % TOTAL

        elif key in (ord(" "), curses.KEY_ENTER, ord("\n"), ord("\r")):
            if cursor < N:
                desired[_DISPLAY_ITEMS[cursor]["app"]["name"]] = not desired[_DISPLAY_ITEMS[cursor]["app"]["name"]]
            elif cursor == ROW_INSTALL:
                if can_install:
                    to_install = [n for n in checked_names
                                  if not status[n]["installed"]]
                    return {"action": "execute",
                            "to_install": to_install, "to_uninstall": []}
            elif cursor == ROW_UNINSTALL:
                if can_uninstall:
                    to_uninstall = [n for n in checked_names
                                    if status[n]["installed"]]
                    return {"action": "execute",
                            "to_install": [], "to_uninstall": to_uninstall}
            elif cursor == ROW_UPGRADE:
                if can_upgrade:
                    return {"action": "upgrade"}
            elif cursor == ROW_PUSH:
                if can_push:
                    return {"action": "push_to_github"}
            elif cursor == ROW_RESTART:
                _flask_checked = [
                    n for n in checked_names
                    if any(a["flask"] and a["name"] == n for a in APPS)
                    and status[n]["installed"]
                ]
                if _flask_checked:
                    return {"action": "execute", "to_install": [], "to_uninstall": [],
                            "to_restart": _flask_checked}
            elif cursor == ROW_REFRESH_STATUS:
                return {"action": "refresh_status"}
            elif cursor == ROW_QUIT:
                return None

        elif key in (ord("i"), ord("e")):
            if can_install:
                to_install = [n for n in checked_names
                              if not status[n]["installed"]]
                return {"action": "execute",
                        "to_install": to_install, "to_uninstall": []}

        elif key == ord("x"):
            if can_uninstall:
                to_uninstall = [n for n in checked_names
                                if status[n]["installed"]]
                return {"action": "execute",
                        "to_install": [], "to_uninstall": to_uninstall}

        elif key == ord("q"):
            return None

        elif key == ord("U"):
            if can_upgrade:
                return {"action": "upgrade"}

        elif key == ord("p"):
            if can_push:
                return {"action": "push_to_github"}

        elif key == ord("f"):
            return {"action": "refresh_status"}

        elif key == ord("R"):
            # Bulk restart — all checked installed Flask apps
            _flask_checked = [
                item["app"]["name"] for item in _DISPLAY_ITEMS
                if item["app"]["flask"]
                and status[item["app"]["name"]]["installed"]
                and desired[item["app"]["name"]]
            ]
            if _flask_checked:
                return {"action": "execute", "to_install": [], "to_uninstall": [],
                        "to_restart": _flask_checked}

        elif key == ord("r"):
            # Single-app restart (cursor position)
            if cursor < N:
                _app  = _DISPLAY_ITEMS[cursor]["app"]
                _name = _app["name"]
                if _app["flask"] and status[_name]["installed"]:
                    return {"action": "execute", "to_install": [], "to_uninstall": [],
                            "to_restart": [_name]}


def run_selection_menu(status: dict[str, dict]) -> Optional[dict]:
    """Launch the curses menu, restoring terminal state on any exception."""
    return curses.wrapper(_curses_menu, status)




# ─────────────────────────────────────────────────────────────────────────────
# OS detection
# ─────────────────────────────────────────────────────────────────────────────
def detect_os() -> str:
    """Return one of: 'macos' 'kali' 'ubuntu' 'rhel' 'suse' 'windows' 'linux'"""
    if SYSTEM == "Darwin":  return "macos"
    if SYSTEM == "Windows": return "windows"
    try:
        txt = Path("/etc/os-release").read_text(errors="ignore").lower()
        for token, key in [
            ("kali",     "kali"),
            ("ubuntu",   "ubuntu"),
            ("red hat",  "rhel"), ("centos", "rhel"),
            ("rocky",    "rhel"), ("alma",   "rhel"), ("fedora", "rhel"),
            ("suse",     "suse"), ("opensuse", "suse"),
        ]:
            if token in txt:
                return key
    except Exception:
        pass
    return "linux"


# ─────────────────────────────────────────────────────────────────────────────
# Package extraction from install.py via AST (no code execution)
# ─────────────────────────────────────────────────────────────────────────────
_PKG_VARS = {
    "APT_PACKAGES", "DNF_PACKAGES", "ZYPPER_PACKAGES",
    "BREW_PACKAGES", "BREW_CASK_PACKAGES",
    "PIP_PACKAGES", "GO_TOOLS", "GIT_TOOLS",
}


def _extract_packages_ast(install_dir: Path) -> dict:
    """Parse an install.py with AST and return its package-list variables."""
    installer = install_dir / "install.py"
    if not installer.exists():
        return {}
    try:
        tree = ast.parse(installer.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    result: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in _PKG_VARS:
                    try:
                        result[t.id] = ast.literal_eval(node.value)
                    except Exception:
                        pass
    return result


def _extract_offline_bundles(install_dir: Path) -> dict:
    """Parse OFFLINE_BUNDLE_URLS dict from an install.py via AST."""
    installer = install_dir / "install.py"
    if not installer.exists():
        return {}
    try:
        tree = ast.parse(installer.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "OFFLINE_BUNDLE_URLS":
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        pass
    return {}


def _normalise_pkg_item(key: str, item) -> list:
    """Normalise any package-list entry format to [identifier, description].

    App install.py files use three different formats:
      • flat string   "flask>=3.0.0"
      • list/tuple    ["nmap", "network scanner"]
      • dict          {"pkg": "github.com/.../nuclei@latest", "label": "nuclei"}

    Arsenal phase runners always expect a list/tuple with at least one element.
    """
    if isinstance(item, (list, tuple)):
        return list(item)
    if isinstance(item, str):
        return [item, ""]
    if isinstance(item, dict):
        if key == "GO_TOOLS":
            pkg    = item.get("pkg") or item.get("package") or ""
            binary = (item.get("label") or item.get("binary")
                      or pkg.split("/")[-1].split("@")[0])
            return [pkg, binary]
        if key == "GIT_TOOLS":
            repo = item.get("repo") or item.get("url") or ""
            path = item.get("path") or item.get("local_path") or ""
            deps = item.get("deps") or item.get("pip_deps") or []
            return [repo, path, deps]
        # APT/DNF/PIP/BREW — grab first two string values
        vals = [v for v in item.values() if isinstance(v, str)]
        if len(vals) >= 2:
            return vals[:2]
        return (vals[:1] + [""]) if vals else ["", ""]
    return [str(item), ""]


def _merge_app_phases(app_names: list) -> dict:
    """
    Collect and deduplicate packages from every app in app_names.
    Returns dict keyed by _PKG_VARS entries plus OFFLINE_BUNDLE_URLS.
    """
    merged: dict = {k: [] for k in _PKG_VARS}
    merged["OFFLINE_BUNDLE_URLS"] = {}
    seen:   dict = {k: set() for k in _PKG_VARS}

    for name in app_names:
        d = _install_dir(APP_INDEX[name])
        pkgs = _extract_packages_ast(d)
        for key in _PKG_VARS:
            for item in pkgs.get(key, []):
                norm = _normalise_pkg_item(key, item)
                dk = norm[0] if norm else ""
                if dk and dk not in seen[key]:
                    seen[key].add(dk)
                    merged[key].append(norm)
        merged["OFFLINE_BUNDLE_URLS"].update(_extract_offline_bundles(d))
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Install log (populated by phase runners, consumed by report generator)
# ─────────────────────────────────────────────────────────────────────────────
_INSTALL_LOG: list = []   # list[dict]


def _log(phase: str, name: str, description: str,
         success: bool, stdout: str = "", stderr: str = "", error: str = "") -> None:
    _INSTALL_LOG.append({
        "phase":       phase,
        "name":        name,
        "description": description,
        "success":     success,
        "stdout":      stdout,
        "stderr":      stderr,
        "error":       error,
    })
    sym = f"{_G}[+]{_R}" if success else f"{_RD}[!]{_R}"
    print(f"  {sym}  {name}  —  {description[:60]}", flush=True)
    if not success and (stderr or error):
        snippet = (stderr or error).strip().splitlines()
        # Show first line (Ruby/brew errors put the message there) + last 3 for context.
        # For Python-style tracebacks the error is at the end, so the last 3 still apply.
        if len(snippet) > 4:
            lines = [snippet[0], "  …"] + snippet[-3:]
        else:
            lines = snippet
        for ln in lines:
            print(f"      {_RD}{ln}{_R}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Spinner helper — shows progress while a subprocess runs
# ─────────────────────────────────────────────────────────────────────────────
_SPINNER_FRAMES = ["|", "/", "-", "\\"]


def _spin_run(label: str, cmd: list, **kwargs):
    """Run *cmd* in a background thread while spinning a progress indicator.

    On non-TTY outputs (pipes, files) falls back to a plain subprocess.run so
    logs stay clean.  Raises whatever the subprocess raises (including
    subprocess.TimeoutExpired) so callers' existing except blocks still fire.
    """
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not is_tty:
        return subprocess.run(cmd, **kwargs)

    result_box: list = [None]
    error_box:  list = [None]

    def _worker():
        try:
            result_box[0] = subprocess.run(cmd, **kwargs)
        except Exception as exc:
            error_box[0] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    label_s = label[:55]
    frames  = itertools.cycle(_SPINNER_FRAMES)
    while t.is_alive():
        sys.stdout.write(f"\r  {_C}[{next(frames)}]{_R}  {label_s}")
        sys.stdout.flush()
        time.sleep(0.1)

    # Erase the spinner line; _log() will print the final status on this line.
    sys.stdout.write(f"\r{' ' * 80}\r")
    sys.stdout.flush()
    t.join()

    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


# ─────────────────────────────────────────────────────────────────────────────
# Brew without elevated privileges
# ─────────────────────────────────────────────────────────────────────────────
def _brew_real_user() -> str:
    """Return the non-root user to run brew as, or '' if undetermined."""
    # Primary: SUDO_USER set by sudo
    user = os.environ.get("SUDO_USER", "").strip()
    if user and user != "root":
        return user
    # Fallback: logname reports the login user even inside sudo shells
    try:
        r = subprocess.run(["logname"], capture_output=True, text=True, timeout=3)
        candidate = r.stdout.strip()
        if candidate and candidate != "root":
            return candidate
    except Exception:
        pass
    return ""


def _brew_cmd(brew: str, *args: str) -> "list | None":
    """
    Build a brew command that runs without elevated privileges.
    Returns None if running as root and no safe user can be determined
    (caller must skip the install in that case).
    """
    if os.geteuid() == 0:
        user = _brew_real_user()
        if not user:
            return None
        return ["sudo", "-u", user, "-H", brew, *args]
    return [brew, *args]


# ─────────────────────────────────────────────────────────────────────────────
# Phase runners
# ─────────────────────────────────────────────────────────────────────────────
def _phase_system(merged: dict, os_type: str) -> None:
    """Install system packages via apt / dnf / zypper (Linux) or brew (macOS)."""
    if os_type == "windows":
        warn("System package install not supported on Windows — skipping.")
        return

    if os_type == "macos":
        brew = shutil.which("brew")
        if not brew:
            warn("Homebrew not found — skipping brew phase.")
            warn("  Install from https://brew.sh then re-run Arsenal.")
            return
        print(f"\n{_BLD}── Phase: brew packages ──────────────────────────────{_R}")
        _brew_user_checked = False
        for item in merged.get("BREW_PACKAGES", []):
            pkg, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
            if shutil.which(pkg.split("/")[-1]):
                _log("brew", pkg, desc, True, stdout="already on PATH — skipped")
                continue
            cmd = _brew_cmd(brew, "install", pkg)
            if cmd is None:
                if not _brew_user_checked:
                    warn("Running as root without a resolvable non-root user — "
                         "skipping all brew installs. Re-run with sudo or as the "
                         "original user.")
                    _brew_user_checked = True
                _log("brew", pkg, desc, False,
                     error="Skipped — cannot run brew as root safely")
                continue
            _brew_user_checked = True
            try:
                r = _spin_run(pkg, cmd, capture_output=True, text=True, timeout=300)
                _log("brew", pkg, desc, r.returncode == 0,
                     stdout=r.stdout, stderr=r.stderr)
            except Exception as exc:
                _log("brew", pkg, desc, False, error=str(exc))
        for item in merged.get("BREW_CASK_PACKAGES", []):
            pkg, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
            cmd = _brew_cmd(brew, "install", "--cask", pkg)
            if cmd is None:
                _log("brew-cask", pkg, desc, False,
                     error="Skipped — cannot run brew as root safely")
                continue
            try:
                r = _spin_run(pkg, cmd, capture_output=True, text=True, timeout=300)
                _log("brew-cask", pkg, desc, r.returncode == 0,
                     stdout=r.stdout, stderr=r.stderr)
            except Exception as exc:
                _log("brew-cask", pkg, desc, False, error=str(exc))
        return

    # ── Linux ────────────────────────────────────────────────────────────────
    if os_type in ("kali", "ubuntu", "linux"):
        pm = shutil.which("apt-get") or shutil.which("apt")
        pkg_list_key = "APT_PACKAGES"
    elif os_type == "rhel":
        pm = shutil.which("dnf") or shutil.which("yum")
        pkg_list_key = "DNF_PACKAGES"
    elif os_type == "suse":
        pm = shutil.which("zypper")
        pkg_list_key = "ZYPPER_PACKAGES"
    else:
        pm = shutil.which("apt-get") or shutil.which("apt")
        pkg_list_key = "APT_PACKAGES"

    if not pm:
        warn(f"Package manager not found for {os_type} — skipping system phase.")
        return

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    # apt update + upgrade — once, before any package installs
    if "apt" in pm:
        print(f"\n{_BLD}── Phase: apt update + upgrade ───────────────────────{_R}")
        try:
            subprocess.run([pm, "update", "-qq"], env=env, timeout=180)
        except subprocess.TimeoutExpired:
            warn("apt update timed out — continuing with cached package lists.")
        except Exception as exc:
            warn(f"apt update failed: {exc}")
        try:
            subprocess.run([pm, "upgrade", "-y", "-qq"], env=env, timeout=3600)
        except subprocess.TimeoutExpired:
            warn("apt upgrade timed out after 60 min — continuing with package installs.")
        except Exception as exc:
            warn(f"apt upgrade failed: {exc}")

    print(f"\n{_BLD}── Phase: system packages ({Path(pm).name}) ─────────────{_R}")
    for item in merged.get(pkg_list_key, []):
        pkg, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
        try:
            if "apt" in pm:
                cmd = [pm, "install", "-y", "-q", pkg,
                       "-o", "Dpkg::Options::=--force-confold"]
            elif "dnf" in pm or "yum" in pm:
                cmd = [pm, "install", "-y", "-q", pkg]
            else:
                cmd = [pm, "install", "-y", pkg]
            r = _spin_run(pkg, cmd, capture_output=True, text=True,
                         env=env, timeout=180)
            _log("system", pkg, desc, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
        except Exception as exc:
            _log("system", pkg, desc, False, error=str(exc))


_OFFLINE_REPO_DIR = "/opt/OfflineInstallers"
_OFFLINE_REPO_URL = "https://github.com/pr0t31n/OfflineInstallers"


def _install_from_local_repo_arsenal(pkg_name: str, tarball: str) -> bool:
    """Copy/reassemble a bundle from the already-cloned OfflineInstallers repo.
    Returns True if *tarball* was written successfully."""
    _local_tar = os.path.join(_OFFLINE_REPO_DIR, f"{pkg_name}_offline_kali.tar.gz")
    if os.path.isfile(_local_tar):
        shutil.copy2(_local_tar, tarball)
        return True
    _repo_parts = sorted([
        os.path.join(_OFFLINE_REPO_DIR, f)
        for f in os.listdir(_OFFLINE_REPO_DIR)
        if f.startswith(f"{pkg_name}_offline_kali.part.")
    ])
    if _repo_parts:
        info(f"  Reassembling {len(_repo_parts)} part(s) from cloned repo …")
        with open(tarball, "wb") as _out:
            for _p in _repo_parts:
                with open(_p, "rb") as _f:
                    _out.write(_f.read())
        return True
    warn(f"{pkg_name} not found in cloned OfflineInstallers repo")
    return False


def _install_offline_bundle_arsenal(pkg_name: str, url: str) -> bool:
    """Download a pre-built offline bundle from GitHub and run install_offline.sh.
    Handles both single .tar.gz files and split .part.* files.
    If the OfflineInstallers repo is already cloned at /opt/OfflineInstallers, installs
    directly from there without attempting any network downloads."""
    info(f"Trying offline bundle: {pkg_name} …")
    _tmp = tempfile.mkdtemp(prefix=f"arsenal_bundle_{pkg_name}_")
    _tarball = os.path.join(_tmp, f"{pkg_name}_offline_kali.tar.gz")
    _downloaded = False
    try:
        # Fast path: OfflineInstallers repo already cloned — skip all network attempts
        if os.path.isdir(os.path.join(_OFFLINE_REPO_DIR, ".git")):
            info(f"OfflineInstallers repo already cloned — installing {pkg_name} directly …")
            _downloaded = _install_from_local_repo_arsenal(pkg_name, _tarball)
        else:
            try:
                urllib.request.urlretrieve(url, _tarball)
                _downloaded = True
            except Exception:
                # Single file unavailable — try split parts (.part.aa, .part.ab, ...)
                _base = url.rsplit("/", 1)[0]
                _parts = []
                _found_gap = False
                for _a in "abcdefghijklmnopqrstuvwxyz":
                    if _found_gap:
                        break
                    for _b in "abcdefghijklmnopqrstuvwxyz":
                        _sfx = _a + _b
                        _pu = f"{_base}/{pkg_name}_offline_kali.part.{_sfx}"
                        _pp = os.path.join(_tmp, f"part.{_sfx}")
                        try:
                            urllib.request.urlretrieve(_pu, _pp)
                            _parts.append(_pp)
                        except Exception:
                            _found_gap = True
                            break
                if _parts:
                    info(f"  Reassembling {len(_parts)} part(s) for {pkg_name} …")
                    with open(_tarball, "wb") as _out:
                        for _p in sorted(_parts):
                            with open(_p, "rb") as _f:
                                _out.write(_f.read())
                    _downloaded = True

            if not _downloaded:
                # Final fallback: clone the OfflineInstallers repo to /opt/ and install from there
                _git = shutil.which("git")
                if _git:
                    info(f"githubusercontent.com inaccessible — cloning OfflineInstallers to /opt/ …")
                    if os.path.isdir(os.path.join(_OFFLINE_REPO_DIR, ".git")):
                        subprocess.run([_git, "-C", _OFFLINE_REPO_DIR, "pull", "--ff-only"],
                                       capture_output=True)
                    else:
                        subprocess.run([_git, "clone", "--depth=1", _OFFLINE_REPO_URL, _OFFLINE_REPO_DIR],
                                       capture_output=True)
                    if os.path.isdir(_OFFLINE_REPO_DIR):
                        _downloaded = _install_from_local_repo_arsenal(pkg_name, _tarball)

        if not _downloaded:
            warn(f"Could not download offline bundle for {pkg_name}")
            return False

        if subprocess.run(["tar", "xzf", _tarball, "-C", _tmp],
                          capture_output=True).returncode != 0:
            warn(f"Failed to extract offline bundle for {pkg_name}")
            return False

        _install_sh = None
        for _root, _dirs, _files in os.walk(_tmp):
            if "install_offline.sh" in _files:
                _install_sh = os.path.join(_root, "install_offline.sh")
                break

        if not _install_sh:
            warn(f"install_offline.sh not found in {pkg_name} bundle")
            return False

        os.chmod(_install_sh, 0o755)
        r_sh = subprocess.run(["bash", _install_sh],
                               capture_output=True, text=True, timeout=300)
        _ok = r_sh.returncode == 0
        _log("bundle", pkg_name, url, _ok, stdout=r_sh.stdout, stderr=r_sh.stderr)
        return _ok
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)


def _phase_pip(merged: dict) -> None:
    print(f"\n{_BLD}── Phase: pip packages ───────────────────────────────{_R}")
    _offline_urls = merged.get("OFFLINE_BUNDLE_URLS", {})
    for item in merged.get("PIP_PACKAGES", []):
        spec, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
        try:
            r = _spin_run(spec,
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--break-system-packages", spec],
                capture_output=True, text=True, timeout=180)
            _log("pip", spec, desc, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
            if r.returncode != 0 and _offline_urls:
                _pkg_name = spec.split(">=")[0].split("==")[0].split("[")[0].strip()
                if _pkg_name in _offline_urls:
                    warn(f"pip failed for {_pkg_name} — trying offline bundle …")
                    _install_offline_bundle_arsenal(_pkg_name, _offline_urls[_pkg_name])
        except Exception as exc:
            _log("pip", spec, desc, False, error=str(exc))


def _phase_go(merged: dict) -> None:
    go = shutil.which("go")
    if not go:
        warn("go not found — skipping Go phase.")
        warn("  Install Go: https://go.dev/dl/")
        return
    env = os.environ.copy()
    if os.geteuid() == 0:
        env["HOME"]   = "/root"
        env["GOPATH"] = "/root/go"
    else:
        env.setdefault("HOME",   str(Path.home()))
        env.setdefault("GOPATH", str(Path(env["HOME"]) / "go"))
    env["GOBIN"] = "/usr/local/bin"
    env.setdefault("GONOSUMDB", "*")
    _offline_urls = merged.get("OFFLINE_BUNDLE_URLS", {})
    print(f"\n{_BLD}── Phase: Go tools ───────────────────────────────────{_R}")
    for item in merged.get("GO_TOOLS", []):
        pkg, binary = (item[0], item[1]) if len(item) >= 2 else (item[0], item[0].split("/")[-1].split("@")[0])
        if shutil.which(binary):
            _log("go", binary, pkg, True, stdout="already on PATH — skipped")
            continue
        try:
            r = _spin_run(binary,
                [go, "install", "-v", pkg],
                capture_output=True, text=True, env=env, timeout=600)
            _log("go", binary, pkg, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
            if r.returncode != 0 and binary in _offline_urls:
                warn(f"go install failed for {binary} — trying offline bundle …")
                _install_offline_bundle_arsenal(binary, _offline_urls[binary])
        except subprocess.TimeoutExpired:
            _log("go", binary, pkg, False, error="go install timed out after 600s")
            if binary in _offline_urls:
                warn(f"go install timed out for {binary} — trying offline bundle …")
                _install_offline_bundle_arsenal(binary, _offline_urls[binary])
        except Exception as exc:
            _log("go", binary, pkg, False, error=str(exc))


def _phase_git(merged: dict) -> None:
    git = shutil.which("git")
    if not git:
        warn("git not found — skipping Git phase.")
        return
    print(f"\n{_BLD}── Phase: Git tools ──────────────────────────────────{_R}")
    for item in merged.get("GIT_TOOLS", []):
        if len(item) < 2:
            continue
        repo_url, local_path = item[0], item[1]
        pip_deps = item[2] if len(item) >= 3 else []
        tool_name = Path(local_path).name
        local = Path(local_path)
        try:
            if local.exists():
                r = _git_spin_run(tool_name,
                    [git, "-C", str(local), "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=120, env=_git_env())
                _log("git", tool_name, f"update {repo_url}", r.returncode == 0,
                     stdout=r.stdout, stderr=r.stderr)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                r = _git_spin_run(tool_name,
                    [git, "clone", "--depth=1", _authed_url(repo_url), str(local)],
                    capture_output=True, text=True, timeout=300, env=_git_env())
                _log("git", tool_name, f"clone {repo_url}", r.returncode == 0,
                     stdout=r.stdout, stderr=r.stderr)
                if r.returncode != 0:
                    continue
        except Exception as exc:
            _log("git", tool_name, repo_url, False, error=str(exc))
            continue
        # pip deps for this git tool
        for dep in (pip_deps or []):
            try:
                r2 = _spin_run(dep,
                    [sys.executable, "-m", "pip", "install", "--quiet",
                     "--break-system-packages", dep],
                    capture_output=True, text=True, timeout=120)
                _log("pip", dep, f"dep for {tool_name}", r2.returncode == 0,
                     stdout=r2.stdout, stderr=r2.stderr)
            except Exception as exc2:
                _log("pip", dep, f"dep for {tool_name}", False, error=str(exc2))


# ─────────────────────────────────────────────────────────────────────────────
# Phased installer (replaces per-app _run_install_py for installs)
# ─────────────────────────────────────────────────────────────────────────────
def install_apps_phased(app_names: list) -> None:
    """
    Install one or more apps using a combined phased approach:
      apt/dnf/zypper (once) → brew → pip → go → git
    Packages are merged and deduplicated across all apps before execution.
    """
    _INSTALL_LOG.clear()
    os_type = detect_os()
    info(f"Detected OS: {os_type}")

    # Step 0: ensure GitHub credentials are cached before any git operation
    _ensure_gh_creds()
    _git_e = _git_env()

    # Step 1: clone / pull all repos first
    for name in app_names:
        app         = APP_INDEX[name]
        install_dir = _install_dir(app)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        if install_dir.is_dir():
            info(f"{name} already present — pulling latest…")
            _git_run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                     capture_output=True, text=True, env=_git_e)
        else:
            info(f"Cloning {app['github_url']}  →  {install_dir}")
            r = _git_run(
                ["git", "clone", "--depth=1", _authed_url(app["github_url"]), str(install_dir)],
                capture_output=True, text=True, env=_git_e,
            )
            if r.returncode != 0:
                err(f"Clone failed for {name}: {r.stderr.strip()[:120]}")
                _log("git", name, f"clone {app['github_url']}", False,
                     stderr=r.stderr, error="git clone failed")

    # Step 2: merge packages from all app installers
    info("Parsing install.py files and merging package lists…")
    merged = _merge_app_phases(app_names)

    total = sum(len(merged[k]) for k in _PKG_VARS)
    info(f"Total unique dependencies across {len(app_names)} app(s): {total}")

    # Step 3: run phases in order
    _phase_system(merged, os_type)
    if os_type != "macos":
        # brew was handled inside _phase_system for macOS; on Linux brew is rare
        pass
    _phase_pip(merged)
    _phase_go(merged)
    _phase_git(merged)

    # Step 4: run any app-specific post-install steps via install.py
    for name in app_names:
        install_dir = _install_dir(APP_INDEX[name])
        installer   = install_dir / "install.py"
        if installer.exists():
            info(f"Running post-install steps from {name}/install.py…")
            try:
                r = subprocess.run(
                    [sys.executable, str(installer),
                     "--skip-apt", "--skip-pip", "--skip-go", "--skip-git"],
                    cwd=str(install_dir),
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode == 0:
                    ok(f"{name} post-install complete.")
                else:
                    warn(f"{name} post-install returned {r.returncode}.")
                    # Combine stdout + stderr so nothing is lost; show last 30 lines
                    combined = []
                    if r.stdout:
                        combined += r.stdout.strip().splitlines()
                    if r.stderr:
                        combined += r.stderr.strip().splitlines()
                    for ln in combined[-30:]:
                        print(f"    {_Y}{ln}{_R}", flush=True)
            except subprocess.TimeoutExpired:
                warn(f"{name} post-install timed out.")
            except Exception as exc:
                warn(f"{name} post-install error: {exc}")

    # Step 5: start services for newly installed apps (Linux / systemd only)
    if shutil.which("systemctl") and os_type not in ("macos", "windows"):
        for name in app_names:
            svc = APP_INDEX[name].get("service")
            if not svc:
                continue
            info(f"Starting {svc}…")
            rs = subprocess.run(["systemctl", "start", svc],
                                capture_output=True, text=True)
            if rs.returncode == 0:
                ok(f"{svc} started.")
            else:
                warn(f"systemctl start {svc} returned {rs.returncode}.")
                if rs.stderr:
                    for ln in rs.stderr.strip().splitlines()[-6:]:
                        print(f"    {_Y}{ln}{_R}", flush=True)

    ok(f"Phased install complete for: {', '.join(app_names)}")


# ─────────────────────────────────────────────────────────────────────────────
# Installation report (Word .docx + plain-text fallback)
# ─────────────────────────────────────────────────────────────────────────────
def _report_path(ext: str) -> Path:
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    home = Path(os.path.expanduser("~"))
    return home / f"arsenal_install_report_{ts}.{ext}"


def _write_text_report(log: list, to_install: list,
                        to_uninstall: list, to_restart: list) -> Path:
    path = _report_path("txt")
    now  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok_n  = sum(1 for e in log if e["success"])
    fail_n = sum(1 for e in log if not e["success"])

    lines = [
        "=" * 70,
        "  ARSENAL INSTALLATION REPORT",
        f"  Generated : {now}",
        f"  Platform  : {SYSTEM} — {detect_os()}",
        f"  Installed : {', '.join(to_install)  or '(none)'}",
        f"  Uninstalled: {', '.join(to_uninstall) or '(none)'}",
        f"  Refreshed : {', '.join(to_restart)  or '(none)'}",
        "=" * 70,
        "",
        f"  Total dependencies attempted: {len(log)}",
        f"  Successful : {ok_n}",
        f"  Failed     : {fail_n}",
        "",
    ]
    phases_seen: list = []
    for entry in log:
        ph = entry["phase"]
        if ph not in phases_seen:
            phases_seen.append(ph)
            lines += ["", f"── Phase: {ph} " + "─" * (50 - len(ph))]
        sym = "[+]" if entry["success"] else "[!]"
        lines.append(f"  {sym}  {entry['name']}  —  {entry['description']}")

    if fail_n:
        lines += ["", "=" * 70, "  FAILURES", "=" * 70, ""]
        idx = 1
        for entry in log:
            if entry["success"]:
                continue
            lines += [
                f"  {idx}. Phase: {entry['phase']}  |  Package: {entry['name']}",
                f"     Description: {entry['description']}",
            ]
            if entry["error"]:
                lines.append(f"     Error: {entry['error']}")
            if entry["stderr"]:
                for ln in entry["stderr"].strip().splitlines()[-20:]:
                    lines.append(f"     | {ln}")
            lines.append("")
            idx += 1

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_docx_report(log: list, to_install: list,
                        to_uninstall: list, to_restart: list) -> Path:
    from docx import Document                           # type: ignore
    from docx.shared import Pt, RGBColor                # type: ignore
    from docx.enum.text import WD_ALIGN_PARAGRAPH       # type: ignore

    doc  = Document()
    now  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok_n  = sum(1 for e in log if e["success"])
    fail_n = sum(1 for e in log if not e["success"])

    # ── Title ─────────────────────────────────────────────────────────────────
    title = doc.add_heading("Arsenal Installation Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph(f"Generated:    {now}")
    doc.add_paragraph(f"Platform:     {SYSTEM} — {detect_os()}")
    doc.add_paragraph(f"Installed:    {', '.join(to_install)  or '(none)'}")
    doc.add_paragraph(f"Uninstalled:  {', '.join(to_uninstall) or '(none)'}")
    doc.add_paragraph(f"Refreshed:    {', '.join(to_restart)  or '(none)'}")

    # ── Summary ───────────────────────────────────────────────────────────────
    doc.add_heading("Summary", level=1)
    tbl = doc.add_table(rows=4, cols=2)
    tbl.style = "Table Grid"
    for row, label, val in [
        (0, "Total dependencies attempted", str(len(log))),
        (1, "Successful",                   str(ok_n)),
        (2, "Failed",                        str(fail_n)),
        (3, "Pass rate",
            f"{100*ok_n//len(log)}%" if log else "N/A"),
    ]:
        tbl.rows[row].cells[0].text = label
        cell = tbl.rows[row].cells[1]
        cell.text = val
        if row in (2, 3) and fail_n > 0:
            for run in cell.paragraphs[0].runs:
                run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

    # ── Detailed results ──────────────────────────────────────────────────────
    doc.add_heading("Detailed Results", level=1)
    phases_seen: list = []
    for entry in log:
        ph = entry["phase"]
        if ph not in phases_seen:
            phases_seen.append(ph)
            doc.add_heading(f"Phase: {ph}", level=2)
        p   = doc.add_paragraph(style="List Bullet")
        sym = "✓" if entry["success"] else "✗"
        run = p.add_run(f"{sym}  {entry['name']}")
        run.bold = True
        run.font.color.rgb = (RGBColor(0x00, 0x70, 0x00) if entry["success"]
                               else RGBColor(0xC0, 0x00, 0x00))
        if entry["description"]:
            p.add_run(f"  —  {entry['description']}")

    # ── Failures summary ──────────────────────────────────────────────────────
    if fail_n:
        doc.add_heading("Failures Detail", level=1)
        idx = 1
        for entry in log:
            if entry["success"]:
                continue
            doc.add_heading(f"{idx}. {entry['name']}  [{entry['phase']}]", level=2)
            if entry["description"]:
                doc.add_paragraph(f"Description: {entry['description']}")
            if entry["error"]:
                doc.add_paragraph(f"Error: {entry['error']}")
            if entry["stderr"]:
                snippet = "\n".join(entry["stderr"].strip().splitlines()[-30:])
                p = doc.add_paragraph(style="No Spacing")
                run = p.add_run(snippet)
                run.font.name = "Courier New"
                run.font.size = Pt(8)
            idx += 1

    path = _report_path("docx")
    doc.save(str(path))
    return path


def generate_report(log: list, to_install: list,
                    to_uninstall: list, to_restart: list) -> None:
    """Generate installation report; .docx preferred, .txt fallback."""
    if not log:
        return
    try:
        import docx  # noqa: F401 — probe only
        path = _write_docx_report(log, to_install, to_uninstall, to_restart)
        ok(f"Installation report (Word): {path}")
    except ImportError:
        warn("python-docx not installed — writing plain-text report.")
        warn("  To enable Word reports: pip install python-docx"
             " --break-system-packages")
        path = _write_text_report(log, to_install, to_uninstall, to_restart)
        ok(f"Installation report (text): {path}")
    except Exception as exc:
        warn(f"Report generation error: {exc} — writing plain-text fallback.")
        path = _write_text_report(log, to_install, to_uninstall, to_restart)
        ok(f"Installation report (text): {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Check-for-updates and upgrade helpers (used from main loop)
# ─────────────────────────────────────────────────────────────────────────────
def _do_check_updates(status: dict) -> None:
    """Query GitHub for latest versions of installed apps; mutate status in place."""
    info("Checking GitHub for latest versions…")
    for app in APPS:
        name = app["name"]
        if not status[name]["installed"]:
            continue
        gv = get_github_version(app)
        st = status[name]
        st["github_ver"] = gv
        lv = st.get("local_ver")
        st["needs_update"] = bool(lv and gv and lv not in ("unknown", gv))

    outdated = [a for a in APPS if status[a["name"]]["needs_update"]]
    if not outdated:
        ok("All installed apps are up to date.")
    else:
        print(f"\n{_Y}Updates available:{_R}")
        for app in outdated:
            st = status[app["name"]]
            print(f"  {_Y}▲{_R}  {app['name']:<15}  "
                  f"{st['local_ver']}  →  {_G}{st['github_ver']}{_R}")


def _do_upgrade(status: dict) -> None:
    """Upgrade every installed app marked needs_update; mutate status in place."""
    outdated = [a for a in APPS if status[a["name"]]["needs_update"]]
    if not outdated:
        ok("All installed apps are up to date.")
        return
    print(f"\n{_BLD}Upgrading:{_R}")
    for app in outdated:
        print(f"  {_Y}▲{_R}  {app['name']}")
    print()
    for app in outdated:
        _update_app(app)
        status[app["name"]]["local_ver"]    = get_local_version(app)
        status[app["name"]]["needs_update"] = False
    ok("Upgrade complete.")


# ─────────────────────────────────────────────────────────────────────────────
# Git push helpers
# ─────────────────────────────────────────────────────────────────────────────
def _push_app_to_github(app: dict, status: dict) -> None:
    """Run git add *, commit, and push origin main from within the app's repo dir."""
    install_dir = _install_dir(app)
    name        = app["name"]
    info(f"Pushing {name}…")

    git_e       = _git_env()
    commit_msg  = ("Changes were made. Things occured. "
                   "This push was automated using Arsenal.")

    r1 = _git_run(
        ["git", "-C", str(install_dir), "add", "*"],
        capture_output=True, text=True, env=git_e,
    )
    if r1.returncode != 0:
        warn(f"{name}: git add returned {r1.returncode} — {r1.stderr.strip()[:80]}")

    r2 = _git_run(
        ["git", "-C", str(install_dir), "commit", "-m", commit_msg],
        capture_output=True, text=True, env=git_e,
    )
    if r2.returncode != 0:
        # Exit code 1 with "nothing to commit" is not an error
        if "nothing to commit" in (r2.stdout + r2.stderr).lower():
            warn(f"{name}: nothing to commit — skipping push.")
            return
        warn(f"{name}: git commit returned {r2.returncode} — {r2.stderr.strip()[:80]}")

    r3 = _git_run(
        ["git", "-C", str(install_dir), "push", "origin", "main"],
        capture_output=True, text=True, env=git_e,
    )
    if r3.returncode == 0:
        ok(f"{name} pushed successfully.")
        status[name]["github_ver"]   = status[name].get("local_ver")
        status[name]["needs_update"] = False
    else:
        err(f"{name}: git push failed — {r3.stderr.strip()[:120]}")


def _do_push_to_github(status: dict) -> None:
    """Push repos where local version is ahead of GitHub; prompt for confirmation."""
    pushable = [
        a for a in APPS
        if status[a["name"]]["installed"]
        and _version_gt(
            status[a["name"]].get("local_ver") or "",
            status[a["name"]].get("github_ver") or "",
        )
    ]

    if not pushable:
        ok("No repos with local version ahead of GitHub.")
        return

    print(f"\n{_Y}Repos with local version ahead of GitHub:{_R}")
    for app in pushable:
        st = status[app["name"]]
        print(f"  {_Y}↑{_R}  {app['name']:<15}  "
              f"local: {st['local_ver']}  →  github: {st.get('github_ver') or '?'}")
    print()

    try:
        ans = input(f"{_BLD}Push all to GitHub? [y/N]{_R} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans != "y":
        info("Aborted.")
        return

    print()
    for app in pushable:
        _push_app_to_github(app, status)
    ok("Push complete.")


# ─────────────────────────────────────────────────────────────────────────────
# Execute selected changes
# ─────────────────────────────────────────────────────────────────────────────
def execute_changes(selection: dict, status: dict[str, dict]) -> None:
    to_install   = selection.get("to_install",   [])
    to_uninstall = selection.get("to_uninstall", [])
    to_restart   = selection.get("to_restart",   [])

    if not to_install and not to_uninstall and not to_restart:
        info("No changes selected.")
        return

    # Pure restart — skip install/uninstall confirmation
    if to_restart and not to_install and not to_uninstall:
        for name in to_restart:
            print(f"{_BLD}{'─' * 50}{_R}")
            print(f"{_BLD}  Restarting {name}{_R}")
            print(f"{_BLD}{'─' * 50}{_R}")
            restart_app(APP_INDEX[name])
            print()
        ok("All operations complete.")
        return

    to_install, to_uninstall = resolve_dependencies(to_install, to_uninstall, status)

    if to_install:
        print(f"\n{_BLD}To install:{_R}")
        for name in to_install:
            port = APP_INDEX[name]["port"]
            port_s = f"  (port {port})" if port else ""
            print(f"  {_G}+{_R}  {name}{port_s}")

    if to_uninstall:
        print(f"\n{_BLD}To uninstall:{_R}")
        for name in to_uninstall:
            print(f"  {_RD}-{_R}  {name}")

    if to_restart:
        print(f"\n{_BLD}To restart:{_R}")
        for name in to_restart:
            print(f"  {_C}↻{_R}  {name}")

    print()
    try:
        ans = input(f"{_BLD}Proceed? [y/N]{_R} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        info("Aborted.")
        return

    if ans != "y":
        info("Aborted.")
        return

    print()
    # ── Installs — phased across all selected apps ──────────────────────
    if to_install:
        _INSTALL_LOG.clear()
        print(f"\n{_BLD}{'─' * 50}{_R}")
        print(f"{_BLD}  Installing (phased): {', '.join(to_install)}{_R}")
        print(f"{_BLD}{'─' * 50}{_R}")
        install_apps_phased(to_install)
        print()
        generate_report(_INSTALL_LOG, to_install, to_uninstall, to_restart)

    for name in to_uninstall:
        print(f"{_BLD}{'─' * 50}{_R}")
        print(f"{_BLD}  Uninstalling {name}{_R}")
        print(f"{_BLD}{'─' * 50}{_R}")
        uninstall_app(APP_INDEX[name])
        print()

    for name in to_restart:
        print(f"{_BLD}{'─' * 50}{_R}")
        print(f"{_BLD}  Restarting {name}{_R}")
        print(f"{_BLD}{'─' * 50}{_R}")
        restart_app(APP_INDEX[name])
        print()

    ok("All operations complete.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    check_root()

    # Banner
    print(f"{_C}{BANNER}{_R}")
    print(f"{_BLD}  Tool installer and manager  ·  Arsenal v{VERSION}{_R}")
    print(f"  Platform: {SYSTEM}   Install base: {INSTALL_BASE}\n")

    # Prompt for GitHub credentials once for the entire session
    _ensure_gh_creds()

    # Verify GitHub reachability and credential validity before loading versions
    _check_github_connection()

    # Initial status load
    status = load_status()
    print_status_table(status)

    # Main loop — stays in menu until the user chooses Quit
    while True:
        selection = run_selection_menu(status)
        print()

        if selection is None:
            info("Exited without changes.")
            break

        action = selection.get("action", "execute")

        if action == "refresh_status":
            info("Refreshing installed / installed version / latest / running …")
            refresh_local_status(status, with_github=True)
            # loop straight back to menu — no pause needed

        elif action == "check_updates":
            _do_check_updates(status)
            try:
                input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")
            except (EOFError, KeyboardInterrupt):
                print()

        elif action == "upgrade":
            _do_upgrade(status)
            try:
                input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")
            except (EOFError, KeyboardInterrupt):
                print()

        elif action == "push_to_github":
            _do_push_to_github(status)
            # Refresh GitHub versions after push so status table is accurate
            for _app in APPS:
                _name = _app["name"]
                if status[_name]["installed"]:
                    _gv = get_github_version(_app)
                    status[_name]["github_ver"] = _gv
                    _lv = status[_name].get("local_ver")
                    status[_name]["needs_update"] = bool(
                        _lv and _gv and _lv not in ("unknown", _gv)
                    )
            try:
                input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")
            except (EOFError, KeyboardInterrupt):
                print()

        else:  # execute — install / uninstall / restart
            execute_changes(selection, status)
            # Refresh status entries for apps that changed
            for name in selection.get("to_install", []):
                app = APP_INDEX[name]
                status[name]["installed"]      = _install_dir(app).is_dir()
                status[name]["local_ver"]      = get_local_version(app)
                status[name]["service_status"] = get_service_status(app)
                status[name]["needs_update"]   = False
            for name in selection.get("to_uninstall", []):
                status[name]["installed"]      = False
                status[name]["local_ver"]      = None
                status[name]["service_status"] = ""
                status[name]["needs_update"]   = False
            _flask_restarted = [n for n in selection.get("to_restart", []) if APP_INDEX[n].get("flask")]
            if _flask_restarted:
                time.sleep(4)
            for name in selection.get("to_restart", []):
                app = APP_INDEX[name]
                if app.get("flask"):
                    status[name]["service_status"] = get_service_status(app)
                    status[name]["running_ver"]    = get_running_version(app)
            try:
                input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")
            except (EOFError, KeyboardInterrupt):
                print()


if __name__ == "__main__":
    main()
