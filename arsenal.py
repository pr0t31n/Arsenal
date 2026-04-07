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

VERSION      = "1.2.0"
INSTALL_BASE = Path("/opt/pr0t31n")
SYSTEM       = platform.system()   # 'Linux' | 'Darwin'

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
        "port":        5002,
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
        "port":        None,
        "flask":       False,
        "main_file":   "aether.py",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Spectre",
        "repo":        "Spectre",
        "github_url":  "https://github.com/pr0t31n/Spectre.git",
        "description": "Web dashboard for Aether wireless assessments — real-time phase tracking, findings streaming, and session management via browser.",
        "port":        5300,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-spectre",
        "requires":    ["Aether"],
    },
    {
        "name":        "Gearbox",
        "repo":        "Gearbox",
        "github_url":  "https://github.com/pr0t31n/Gearbox.git",
        "description": "Hardware security assessment CLI — orchestrates nmap, Nessus, sslscan, nuclei, Responder, and 20+ tools into structured pentest workflows.",
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
        "port":        5000,
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
        "port":        None,
        "flask":       False,
        "main_file":   "main.go",
        "service":     None,
        "requires":    [],
    },
    {
        "name":        "Nexus",
        "repo":        "Nexus",
        "github_url":  "https://github.com/pr0t31n/Nexus.git",
        "description": "Central hub — discovers and monitors all pr0t31n and third-party security tool instances across every network interface in real time.",
        "port":        5400,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-nexus",
        "requires":    [],
    },
]

APP_INDEX: dict[str, dict] = {a["name"]: a for a in APPS}


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
def get_local_version(app: dict) -> Optional[str]:
    """Read VERSION constant from the installed application source."""
    install_dir = INSTALL_BASE / app["repo"]
    if not install_dir.is_dir():
        return None

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


def get_github_version(app: dict) -> Optional[str]:
    """Query GitHub API for the latest release or tag."""
    repo = app["repo"]
    hdrs = {"User-Agent": f"Arsenal/{VERSION}", "Accept": "application/vnd.github+json"}

    # Try releases/latest first
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

    # Fall back to tags
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
# Service status
# ─────────────────────────────────────────────────────────────────────────────
def get_service_status(app: dict) -> str:
    """Return the current service status string for a Flask app."""
    svc = app.get("service")
    if not svc:
        return ""
    try:
        if SYSTEM == "Linux":
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=4,
            )
            return r.stdout.strip() or "unknown"
        if SYSTEM == "Darwin":
            label = f"com.pr0t31n.{app['name'].lower()}"
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=4,
            )
            return "running" if r.returncode == 0 else "stopped"
    except Exception:
        pass
    return "unknown"



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
    install_dir = INSTALL_BASE / app["repo"]
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
        installed  = (INSTALL_BASE / app["repo"]).is_dir()
        local_ver  = get_local_version(app) if installed else None
        github_ver = get_github_version(app)
        needs_upd  = bool(
            installed
            and local_ver
            and github_ver
            and local_ver not in ("unknown", github_ver)
        )
        svc_status = (get_service_status(app)
                      if installed and app["flask"] else "")
        status[app["name"]] = {
            "installed":      installed,
            "local_ver":      local_ver,
            "github_ver":     github_ver,
            "needs_update":   needs_upd,
            "service_status": svc_status,
        }
    return status


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
    install_dir = INSTALL_BASE / app["repo"]
    info(f"Updating {app['name']}…")
    r = subprocess.run(["git", "-C", str(install_dir), "pull", "--ff-only"])
    if r.returncode == 0:
        _run_install_py(install_dir)
        ok(f"{app['name']} updated.")
    else:
        err(f"git pull failed for {app['name']}.")


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
    """Clone repo into INSTALL_BASE and run install.py."""
    INSTALL_BASE.mkdir(parents=True, exist_ok=True)
    install_dir = INSTALL_BASE / app["repo"]

    if install_dir.is_dir():
        info(f"{app['name']} already present — pulling latest…")
        subprocess.run(["git", "-C", str(install_dir), "pull", "--ff-only"])
    else:
        info(f"Cloning {app['github_url']}  →  {install_dir}")
        r = subprocess.run([
            "git", "clone", "--depth=1", app["github_url"], str(install_dir),
        ])
        if r.returncode != 0:
            err(f"Clone failed for {app['name']}.")
            return

    _run_install_py(install_dir)
    ok(f"{app['name']} installed.")


def uninstall_app(app: dict) -> None:
    """Stop/disable service (if any), remove symlinks, delete install directory."""
    install_dir = INSTALL_BASE / app["repo"]

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
        if lk.is_symlink() and str(lk.resolve()).startswith(str(INSTALL_BASE)):
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
_CP_HEADER   = 1   # cyan
_CP_NORMAL   = 2   # white
_CP_HILITE   = 3   # black on yellow (selected row)
_CP_CHECKED  = 4   # green  (installed + keeping)
_CP_UNCKD    = 5   # dim    (not installed, not selected)
_CP_TO_UNINST= 6   # red    (marked to uninstall)
_CP_TO_INST  = 7   # bright green (marked to install)
_CP_EXEC_BTN = 8   # bold green
_CP_QUIT_BTN     = 9   # bold red
_CP_REFRESH_BTN  = 10  # bold cyan


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_CP_HEADER,    curses.COLOR_CYAN,   -1)
    curses.init_pair(_CP_NORMAL,    curses.COLOR_WHITE,  -1)
    curses.init_pair(_CP_HILITE,    curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(_CP_CHECKED,   curses.COLOR_GREEN,  -1)
    curses.init_pair(_CP_UNCKD,     curses.COLOR_WHITE,  -1)
    curses.init_pair(_CP_TO_UNINST, curses.COLOR_RED,    -1)
    curses.init_pair(_CP_TO_INST,   curses.COLOR_GREEN,  -1)
    curses.init_pair(_CP_EXEC_BTN,  curses.COLOR_GREEN,  -1)
    curses.init_pair(_CP_QUIT_BTN,     curses.COLOR_RED,    -1)
    curses.init_pair(_CP_REFRESH_BTN,  curses.COLOR_CYAN,   -1)


# ─────────────────────────────────────────────────────────────────────────────
# Interactive curses selection menu
# ─────────────────────────────────────────────────────────────────────────────
def _curses_menu(
    stdscr: "curses._CursesWindow",
    status: dict[str, dict],
) -> Optional[dict]:
    """
    Interactive checkbox list.
    Returns {"to_install": [...], "to_uninstall": [...]} or None (quit).

    Keys:
        ↑ / k        — move up
        ↓ / j        — move down
        SPACE / ENTER — toggle checkbox
        e            — execute (from anywhere)
        q            — quit without changes
    """
    curses.curs_set(0)
    _init_colors()

    N = len(APPS)

    # Desired end-state per app (True = want installed)
    desired: dict[str, bool] = {
        a["name"]: status[a["name"]]["installed"] for a in APPS
    }

    # Navigation: rows 0..N-1 = apps, row N = Execute, row N+1 = Quit
    ROW_EXEC    = N
    ROW_REFRESH = N + 1
    ROW_QUIT    = N + 2
    TOTAL       = N + 3
    cursor   = 0
    scroll   = 0      # index of first visible app row in viewport

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        safe_x = max_x - 1

        if max_y < 12 or max_x < 50:
            stdscr.addstr(0, 0, "Terminal too small — resize and retry.")
            stdscr.refresh()
            stdscr.getch()
            return None

        # ── column header ────────────────────────────────────────────────────
        hdr1 = f"  Arsenal v{VERSION} — Tool Selection"
        stdscr.addstr(0, 0, hdr1[:safe_x],
                      curses.color_pair(_CP_HEADER) | curses.A_BOLD)

        col_hdr = (
            f"  {'':3}  {'Application':<14}  "
            f"{'Installed':<9}  {'Latest':<9}  {'Port':<6}  Description"
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

            app  = APPS[row_idx]
            name = app["name"]
            st   = status[name]
            d    = desired[name]
            orig = st["installed"]

            # Checkbox marker and colour
            if d and not orig:
                chk, chk_cp = "[+]", _CP_TO_INST
            elif not d and orig:
                chk, chk_cp = "[-]", _CP_TO_UNINST
            elif d and orig:
                chk, chk_cp = "[✓]", _CP_CHECKED
            else:
                chk, chk_cp = "[ ]", _CP_UNCKD

            lv   = st["local_ver"]  or "—"
            gv   = st["github_ver"] or "?"
            port = str(app["port"]) if app["port"] else "—"

            # Truncate description to remaining width
            desc_start = 2 + 5 + 14 + 9 + 9 + 6 + 6
            desc_w     = max(10, max_x - desc_start - 2)
            desc       = app["description"][:desc_w]

            row_text = (
                f"  {chk}  {name:<14}  {lv:<9}  {gv:<9}  {port:<6}  {desc}"
            )

            is_on = (row_idx == cursor)
            row_attr = (curses.color_pair(_CP_HILITE)
                        if is_on else curses.color_pair(_CP_NORMAL))
            try:
                stdscr.addstr(screen_y, 0, row_text[:safe_x], row_attr)
                # Re-paint checkbox with its semantic colour
                chk_attr = curses.color_pair(chk_cp) | curses.A_BOLD
                if is_on:
                    chk_attr |= curses.color_pair(_CP_HILITE)
                stdscr.addstr(screen_y, 2, chk, chk_attr)
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

        exec_attr    = ((curses.color_pair(_CP_HILITE) | curses.A_BOLD)
                        if cursor == ROW_EXEC
                        else (curses.color_pair(_CP_EXEC_BTN) | curses.A_BOLD))
        can_refresh  = any(
            APPS[i]["flask"] and status[APPS[i]["name"]]["installed"]
            and desired[APPS[i]["name"]]
            for i in range(N)
        )
        refresh_attr = ((curses.color_pair(_CP_HILITE) | curses.A_BOLD)
                        if cursor == ROW_REFRESH
                        else (curses.color_pair(_CP_REFRESH_BTN) | curses.A_BOLD
                              if can_refresh
                              else curses.color_pair(_CP_UNCKD) | curses.A_DIM))
        quit_attr    = ((curses.color_pair(_CP_HILITE) | curses.A_BOLD)
                        if cursor == ROW_QUIT
                        else (curses.color_pair(_CP_QUIT_BTN) | curses.A_BOLD))
        try:
            stdscr.addstr(fy + 1, 2,  "[ Execute ]",  exec_attr)
            stdscr.addstr(fy + 1, 16, "[ Refresh ]",  refresh_attr)
            stdscr.addstr(fy + 1, 30, "[ Quit ]",     quit_attr)
        except curses.error:
            pass

        try:
            can_restart = (cursor < N and APPS[cursor]["flask"]
                           and status[APPS[cursor]["name"]]["installed"])
            hint = ("  ↑/↓: navigate   SPACE/ENTER: toggle   "
                    "e: execute   R: refresh flask   q: quit"
                    + ("   r: restart this" if can_restart else ""))
            stdscr.addstr(fy + 2, 0, hint[:safe_x], curses.color_pair(_CP_HEADER))
        except curses.error:
            pass

        try:
            req = ("  Dependencies: Spectre→Aether  "
                   "Gauntlet→Gearbox  Vantage→Scattershot3+OSift")
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
                desired[APPS[cursor]["name"]] = not desired[APPS[cursor]["name"]]
            elif cursor == ROW_EXEC:
                break
            elif cursor == ROW_REFRESH:
                _flask_checked = [
                    APPS[i]["name"] for i in range(N)
                    if APPS[i]["flask"]
                    and status[APPS[i]["name"]]["installed"]
                    and desired[APPS[i]["name"]]
                ]
                if _flask_checked:
                    return {"to_install": [], "to_uninstall": [],
                            "to_restart": _flask_checked}
            elif cursor == ROW_QUIT:
                return None

        elif key == ord("e"):
            break

        elif key == ord("q"):
            return None

        elif key == ord("R"):
            # Bulk refresh — all checked installed Flask apps
            _flask_checked = [
                APPS[i]["name"] for i in range(N)
                if APPS[i]["flask"]
                and status[APPS[i]["name"]]["installed"]
                and desired[APPS[i]["name"]]
            ]
            if _flask_checked:
                return {"to_install": [], "to_uninstall": [],
                        "to_restart": _flask_checked}

        elif key == ord("r"):
            # Single-app restart (cursor position)
            if cursor < N:
                _app  = APPS[cursor]
                _name = _app["name"]
                if _app["flask"] and status[_name]["installed"]:
                    return {"to_install": [], "to_uninstall": [],
                            "to_restart": [_name]}

    # Build result lists
    to_install   = [n for n, d in desired.items()
                    if d and not status[n]["installed"]]
    to_uninstall = [n for n, d in desired.items()
                    if not d and status[n]["installed"]]
    return {"to_install": to_install, "to_uninstall": to_uninstall}


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
    Returns dict keyed by _PKG_VARS entries.
    """
    merged: dict = {k: [] for k in _PKG_VARS}
    seen:   dict = {k: set() for k in _PKG_VARS}

    for name in app_names:
        d = INSTALL_BASE / APP_INDEX[name]["repo"]
        pkgs = _extract_packages_ast(d)
        for key in _PKG_VARS:
            for item in pkgs.get(key, []):
                norm = _normalise_pkg_item(key, item)
                dk = norm[0] if norm else ""
                if dk and dk not in seen[key]:
                    seen[key].add(dk)
                    merged[key].append(norm)
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
        for ln in snippet[-4:]:
            print(f"      {_RD}{ln}{_R}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Brew without elevated privileges
# ─────────────────────────────────────────────────────────────────────────────
def _brew_cmd(brew: str, *args: str) -> list:
    """
    Build a brew command that runs without elevated privileges.
    If running as root (sudo), drop back to the original user via sudo -u.
    """
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        return ["sudo", "-u", sudo_user, brew, *args]
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
        for item in merged.get("BREW_PACKAGES", []):
            pkg, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
            if shutil.which(pkg.split("/")[-1]):
                _log("brew", pkg, desc, True, stdout="already on PATH — skipped")
                continue
            try:
                r = subprocess.run(
                    _brew_cmd(brew, "install", pkg),
                    capture_output=True, text=True, timeout=300,
                )
                _log("brew", pkg, desc, r.returncode == 0,
                     stdout=r.stdout, stderr=r.stderr)
            except Exception as exc:
                _log("brew", pkg, desc, False, error=str(exc))
        for item in merged.get("BREW_CASK_PACKAGES", []):
            pkg, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
            try:
                r = subprocess.run(
                    _brew_cmd(brew, "install", "--cask", pkg),
                    capture_output=True, text=True, timeout=300,
                )
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
        subprocess.run([pm, "update",  "-qq"], env=env, timeout=120)
        subprocess.run([pm, "upgrade", "-y", "-qq"], env=env, timeout=300)

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
            r = subprocess.run(cmd, capture_output=True, text=True,
                               env=env, timeout=180)
            _log("system", pkg, desc, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
        except Exception as exc:
            _log("system", pkg, desc, False, error=str(exc))


def _phase_pip(merged: dict) -> None:
    print(f"\n{_BLD}── Phase: pip packages ───────────────────────────────{_R}")
    for item in merged.get("PIP_PACKAGES", []):
        spec, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--break-system-packages", spec],
                capture_output=True, text=True, timeout=180,
            )
            _log("pip", spec, desc, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
        except Exception as exc:
            _log("pip", spec, desc, False, error=str(exc))


def _phase_go(merged: dict) -> None:
    go = shutil.which("go")
    if not go:
        warn("go not found — skipping Go phase.")
        warn("  Install Go: https://go.dev/dl/")
        return
    env = os.environ.copy()
    env.setdefault("HOME",   "/root" if os.geteuid() == 0 else str(Path.home()))
    env["GOBIN"] = "/usr/local/bin"
    env.setdefault("GOPATH", str(Path(env["HOME"]) / "go"))
    print(f"\n{_BLD}── Phase: Go tools ───────────────────────────────────{_R}")
    for item in merged.get("GO_TOOLS", []):
        pkg, binary = (item[0], item[1]) if len(item) >= 2 else (item[0], item[0].split("/")[-1].split("@")[0])
        if shutil.which(binary):
            _log("go", binary, pkg, True, stdout="already on PATH — skipped")
            continue
        try:
            r = subprocess.run(
                [go, "install", "-v", pkg],
                capture_output=True, text=True, env=env, timeout=600,
            )
            _log("go", binary, pkg, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
        except subprocess.TimeoutExpired:
            _log("go", binary, pkg, False, error="go install timed out after 600s")
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
                r = subprocess.run(
                    [git, "-C", str(local), "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=120,
                )
                _log("git", tool_name, f"update {repo_url}", r.returncode == 0,
                     stdout=r.stdout, stderr=r.stderr)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(
                    [git, "clone", "--depth=1", repo_url, str(local)],
                    capture_output=True, text=True, timeout=300,
                )
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
                r2 = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet",
                     "--break-system-packages", dep],
                    capture_output=True, text=True, timeout=120,
                )
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

    # Step 1: clone / pull all repos first
    for name in app_names:
        app         = APP_INDEX[name]
        install_dir = INSTALL_BASE / app["repo"]
        INSTALL_BASE.mkdir(parents=True, exist_ok=True)
        if install_dir.is_dir():
            info(f"{name} already present — pulling latest…")
            subprocess.run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                           capture_output=True)
        else:
            info(f"Cloning {app['github_url']}  →  {install_dir}")
            r = subprocess.run(
                ["git", "clone", "--depth=1", app["github_url"], str(install_dir)],
                capture_output=True, text=True,
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
        install_dir = INSTALL_BASE / APP_INDEX[name]["repo"]
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
                    if r.stderr:
                        print(f"  {_Y}{r.stderr.strip()[:200]}{_R}")
            except subprocess.TimeoutExpired:
                warn(f"{name} post-install timed out.")
            except Exception as exc:
                warn(f"{name} post-install error: {exc}")

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

    # Status
    status = load_status()
    print_status_table(status)

    # Update check (optional interactive step)
    check_updates(status)

    # Prompt before entering curses
    print(f"{_BLD}  Opening selection menu…{_R}  (press any key)")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    # Interactive selection
    selection = run_selection_menu(status)

    # Restore normal terminal output
    print()

    if selection is None:
        info("Exited without changes.")
        return

    execute_changes(selection, status)


if __name__ == "__main__":
    main()
