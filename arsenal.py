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

VERSION      = "1.1.0"
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
                subprocess.run(["launchctl", "stop",  label],
                               check=False, timeout=10)
                r = subprocess.run(["launchctl", "start", label],
                                   check=False, timeout=10)
                if r.returncode == 0:
                    ok(f"{name} restarted via launchctl.")
                    return True
                warn(f"launchctl start {label} failed (code {r.returncode}) "
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
_CP_QUIT_BTN = 9   # bold red


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
    curses.init_pair(_CP_QUIT_BTN,  curses.COLOR_RED,    -1)


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
    TOTAL    = N + 2
    ROW_EXEC = N
    ROW_QUIT = N + 1
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

        exec_attr = ((curses.color_pair(_CP_HILITE) | curses.A_BOLD)
                     if cursor == ROW_EXEC
                     else (curses.color_pair(_CP_EXEC_BTN) | curses.A_BOLD))
        quit_attr = ((curses.color_pair(_CP_HILITE) | curses.A_BOLD)
                     if cursor == ROW_QUIT
                     else (curses.color_pair(_CP_QUIT_BTN) | curses.A_BOLD))
        try:
            stdscr.addstr(fy + 1, 2,  "[ Execute ]", exec_attr)
            stdscr.addstr(fy + 1, 16, "[ Quit ]",    quit_attr)
        except curses.error:
            pass

        try:
            can_restart = (cursor < N and APPS[cursor]["flask"]
                           and status[APPS[cursor]["name"]]["installed"])
            hint = ("  ↑/↓: navigate   SPACE/ENTER: toggle   "
                    "e: execute   q: quit"
                    + ("   r: restart" if can_restart else ""))
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
            elif cursor == ROW_QUIT:
                return None

        elif key == ord("e"):
            break

        elif key == ord("q"):
            return None

        elif key == ord("r"):
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
    for name in to_install:
        print(f"{_BLD}{'─' * 50}{_R}")
        print(f"{_BLD}  Installing {name}{_R}")
        print(f"{_BLD}{'─' * 50}{_R}")
        install_app(APP_INDEX[name])
        print()

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
