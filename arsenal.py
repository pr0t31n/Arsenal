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
import select as _select
import signal

VERSION = Path(__file__).resolve().parent.joinpath("VERSION").read_text().strip()
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


def _parse_token_flag() -> None:
    """Check sys.argv for --token <PAT> and populate _GH_CREDS if present."""
    for i, arg in enumerate(sys.argv):
        if arg == "--token" and i + 1 < len(sys.argv):
            _GH_CREDS["user"] = "token"
            _GH_CREDS["token"] = sys.argv[i + 1]
            _ASKPASS_PATH[0] = None
            return
        if arg.startswith("--token="):
            _GH_CREDS["user"] = "token"
            _GH_CREDS["token"] = arg.split("=", 1)[1]
            _ASKPASS_PATH[0] = None
            return


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


def _clean_pycache(repo_dir: "str | Path") -> bool:
    """Delete all __pycache__ dirs in a repo. Returns True if any were removed."""
    import shutil
    repo_dir = Path(repo_dir)
    removed = False
    for pc in list(repo_dir.rglob("__pycache__")):
        try:
            shutil.rmtree(pc)
            removed = True
        except OSError:
            pass
    return removed


def _reset_runtime_generated(install_dir: "str | Path", git_env: dict) -> bool:
    """Reset scaffold/runtime-generated files that dirty the working tree.

    Before pulling, any tool whose .gitignore covers paths that git still
    tracks (scaffold output, runtime artefacts) needs those paths reset so
    the pull can land cleanly.  Also removes untracked files that would
    collide with incoming changes.
    """
    d = str(install_dir)
    dirty = _git_run(["git", "-C", d, "diff", "--name-only"],
                     capture_output=True, text=True, env=git_env)
    untracked = _git_run(["git", "-C", d, "ls-files", "--others",
                          "--exclude-standard"],
                         capture_output=True, text=True, env=git_env)
    dirty_files = [f for f in (dirty.stdout or "").splitlines() if f.strip()]
    untracked_files = [f for f in (untracked.stdout or "").splitlines() if f.strip()]
    if not dirty_files and not untracked_files:
        return False
    ignored = _git_run(["git", "-C", d, "check-ignore", "--stdin"],
                       input="\n".join(dirty_files) if dirty_files else "",
                       capture_output=True, text=True, env=git_env)
    ignored_set = {f.strip() for f in (ignored.stdout or "").splitlines() if f.strip()}
    if ignored_set:
        info(f"Resetting {len(ignored_set)} runtime-generated file(s) blocking git pull…")
        _git_run(["git", "-C", d, "checkout", "--"] + list(ignored_set),
                 capture_output=True, text=True, env=git_env)
    if untracked_files:
        incoming = _git_run(["git", "-C", d, "diff", "--name-only",
                             "HEAD", "origin/HEAD"],
                            capture_output=True, text=True, env=git_env)
        incoming_set = {f.strip() for f in (incoming.stdout or "").splitlines() if f.strip()}
        collisions = set(untracked_files) & incoming_set
        if collisions:
            info(f"Removing {len(collisions)} untracked file(s) colliding with incoming changes…")
            for fp in collisions:
                full = Path(d) / fp
                try:
                    full.unlink(missing_ok=True)
                except OSError:
                    pass
    return bool(ignored_set) or bool(untracked_files)


def _pycache_conflict_in_stderr(stderr: str) -> bool:
    """True when git pull stderr lists only __pycache__ paths as conflicts."""
    if not stderr:
        return False
    conflict_files = []
    in_block = False
    for line in stderr.splitlines():
        if "would be overwritten by merge" in line or "would be removed by merge" in line:
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if stripped and not stripped.startswith(("Please", "Aborting", "error:")):
                conflict_files.append(stripped)
            else:
                in_block = False
    return bool(conflict_files) and all("__pycache__" in f for f in conflict_files)


def _ensure_filemode_off(install_dir: "str | Path") -> None:
    """Disable core.fileMode so permission-only diffs never block pulls."""
    _git_run(["git", "-C", str(install_dir), "config", "core.fileMode", "false"],
             capture_output=True, text=True)


def _is_mode_only_dirty(install_dir: "str | Path", git_env: dict) -> bool:
    """True when all working-tree changes are permission-only (no content)."""
    r = _git_run(["git", "-C", str(install_dir), "diff", "--summary"],
                 capture_output=True, text=True, env=git_env)
    lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    if not lines:
        return False
    return all(l.startswith("mode change") for l in lines)


def _git_pull_with_pycache_retry(install_dir: "str | Path", git_env: dict) -> "subprocess.CompletedProcess":
    """git pull --ff-only with automatic cleanup on failure.

    Handles common cases on managed servers:
    1. __pycache__ conflicts (gitignored files blocking merge)
    2. Permission-only changes (core.fileMode=true on Linux)
    3. Scaffold/runtime-generated files (gitignored but tracked, or
       untracked collisions with incoming changes)
    4. Diverged history (force-pushed remote, email scrub, etc.)
    """
    _ensure_filemode_off(install_dir)
    is_shallow = (Path(install_dir) / ".git" / "shallow").exists()
    fetch_cmd = ["git", "-C", str(install_dir), "fetch", "--force", "--prune"]
    if is_shallow:
        fetch_cmd.append("--update-shallow")
    fetch_r = _git_run(fetch_cmd, capture_output=True, text=True, env=git_env)
    if fetch_r.returncode != 0:
        if is_shallow:
            info(f"Shallow fetch failed for {Path(install_dir).name} — unshallowing…")
            _git_run(["git", "-C", str(install_dir), "fetch", "--unshallow"],
                     capture_output=True, text=True, env=git_env)
        else:
            warn(f"git fetch failed for {Path(install_dir).name}: {(fetch_r.stderr or '').strip()[:120]}")
    _reset_runtime_generated(install_dir, git_env)
    _clean_pycache(install_dir)
    r = _git_run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                 capture_output=True, text=True, env=git_env)
    if r.returncode != 0:
        recovered = False
        if _pycache_conflict_in_stderr(r.stderr or ""):
            info("Cleaning __pycache__ blocking git pull...")
            _clean_pycache(install_dir)
            recovered = True
        if _is_mode_only_dirty(install_dir, git_env):
            info("Resetting permission-only changes blocking git pull...")
            recovered = True
        if recovered:
            _git_run(["git", "-C", str(install_dir), "checkout", "--", "."],
                     capture_output=True, text=True, env=git_env)
            r = _git_run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                         capture_output=True, text=True, env=git_env)
        if r.returncode != 0 and fetch_r.returncode == 0:
            info(f"Local branch diverged — resetting to origin ({Path(install_dir).name})…")
            branch = _git_run(
                ["git", "-C", str(install_dir), "rev-parse",
                 "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, env=git_env,
            )
            br = (branch.stdout or "").strip() or "main"
            _git_run(
                ["git", "-C", str(install_dir), "reset", "--hard",
                 f"origin/{br}"],
                capture_output=True, text=True, env=git_env,
            )
            _clean_pycache(install_dir)
            r = _git_run(
                ["git", "-C", str(install_dir), "pull", "--ff-only"],
                capture_output=True, text=True, env=git_env,
            )
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


def _clean_input(prompt: str = "") -> str:
    """input() replacement that periodically repaints the prompt line.

    When Arsenal is run over an SSH session that also carries a SOCKS proxy
    (``ssh -D …``), the SSH client writes ``channel N: open failed`` messages
    to the PTY whenever a SOCKS-forwarded connection is refused.  These appear
    inline, garbling whatever prompt Arsenal is showing.

    This function uses ``select`` with a 1.5 s timeout so that every 1.5 s it
    clears the current line and reprints the prompt — painting over any SSH
    noise that arrived since the last repaint.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    repaint = prompt.lstrip("\n")
    try:
        while True:
            if _select.select([sys.stdin], [], [], 1.5)[0]:
                return sys.stdin.readline().rstrip("\n")
            sys.stdout.write(f"\r\033[2K{repaint}")
            sys.stdout.flush()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


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
    # ── All tools (alphabetical) ──
    {
        "name":        "Aether",
        "repo":        "Aether",
        "github_url":  "https://github.com/pr0t31n/Aether.git",
        "description": "25-phase wireless pentest orchestrator — WEP/WPA/WPA2/WPA3: PMKID capture, handshake cracking, WPA-Enterprise credential harvesting, Dragonblood SAE testing, KRACK validation, Evil Twin/KARMA, client-side PMF/BTM attacks, EAP/SAE downgrade, post-connection recon, legacy TKIP/WPS/captive bypass, Meraki iPSK detection, EAP-TLS identity disclosure.",
        "short_desc":  "WiFi",
        "port":        None,
        "flask":       False,
        "main_file":   "aether.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
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
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
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
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Aether-Z",
        "repo":        "Aether-Z",
        "github_url":  "https://github.com/pr0t31n/Aether-Z.git",
        "description": "Five-phase ZigBee / IEEE 802.15.4 pentest orchestrator — RF survey, network key recovery via join intercept, ZDO device enumeration, ZLL touchlink attacks, ZCL replay testing, and firmware analysis. 15-entry AETHZ findings catalogue with Crucible integration.",
        "short_desc":  "ZigBee",
        "port":        None,
        "flask":       False,
        "main_file":   "aether_z.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Anvil",
        "repo":        "Anvil",
        "github_url":  "https://github.com/pr0t31n/Anvil.git",
        "description": "Process State Sweep — local host forensic snapshot: process inventory (root-shell heuristics), listening socket map (high-risk root-bound services), kernel module list, setuid binaries owned by non-root.",
        "short_desc":  "Host PSS",
        "port":        None,
        "flask":       False,
        "main_file":   "anvil.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Arsenal",
        "repo":        "Arsenal",
        "install_path": str(Path(__file__).resolve().parent),
        "github_url":  "https://github.com/pr0t31n/Arsenal.git",
        "description": "Installer and manager for the pr0t31n toolkit — checks, installs, updates, and uninstalls all pr0t31n tools. Live status dashboard, dependency auto-resolution, systemd/launchd service management, JSON output mode, and non-interactive deployments.",
        "short_desc":  "Toolkit Manager",
        "port":        None,
        "flask":       False,
        "main_file":   "arsenal.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Auger",
        "repo":        "Auger",
        "github_url":  "https://github.com/pr0t31n/Auger.git",
        "description": "Read-only database security auditor — 22 engines (MySQL, PostgreSQL, MSSQL, Oracle, MongoDB, Redis, Cassandra, Elasticsearch, CouchDB, InfluxDB, Neo4j, Memcached, Redshift, DynamoDB, RDS, Milvus, Qdrant, Weaviate, ChromaDB, pgvector, SQLite, H2), offline CVE DB, 5 compliance frameworks.",
        "short_desc":  "Database",
        "port":        None,
        "flask":       False,
        "main_file":   "auger.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
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
        "database":  None,
    },
    {
        "name":        "Basalt",
        "repo":        "Basalt",
        "github_url":  "https://github.com/pr0t31n/Basalt.git",
        "description": "Encrypted file-level backup, restore & verify with AES-256-CTR, HMAC-SHA256 authentication, SHA-256 manifest validation, and atomic rollback. Three subcommands (backup, restore, verify). NIST SP 800-132/800-171 compliant.",
        "short_desc":  "Encrypted Backup",
        "port":        None,
        "flask":       False,
        "main_file":   "basalt.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Bastion",
        "repo":        "Bastion",
        "github_url":  "https://github.com/pr0t31n/Bastion.git",
        "description": "Firewall configuration audit — 13 vendor parsers (Cisco ASA/IOS/FTD, Palo Alto, FortiGate, Check Point, SonicWall, WatchGuard, Sophos, Juniper, Cato, Cloudflare, Sangfor), 83 audit rules, NIST 800-41/800-53 and ISO 27001 mapping, network architecture diagrams, change tracking.",
        "short_desc":  "Firewall Audit",
        "port":        None,
        "flask":       False,
        "main_file":   "bastion.py",
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Beacon",
        "repo":        "Beacon",
        "github_url":  "https://github.com/pr0t31n/Beacon.git",
        "description": "Engagement status server — receives push updates from Crucible instances and presents a read-only dashboard with risk pills, tool status, and 60s auto-refresh.",
        "short_desc":  "Status Server",
        "port":        9443,
        "flask":       True,
        "main_file":   "beacon.py",
        "service":     "pr0t31n-beacon",
        "requires":    [],
        "database":  "SQLite (beacon.db) — engagement status snapshots",
    },
    {
        "name":        "Crucible",
        "repo":        "Crucible",
        "github_url":  "https://github.com/pr0t31n/Crucible.git",
        "description": "Unified pentest orchestration platform — manages 18 integrated tool adapters, 13 quick-action tools, and 9 external CLI integrations across 7 engagement scopes. 49 report types, real-time findings streaming, false-positive management, manual testing checklists, mobile intelligence, and distributed worker delegation.",
        "short_desc":  "Tool Orchestrator",
        "port":        5100,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-crucible",
        "requires":    ["Shared"],
        "managed":     [
            "Aether", "Aether-B", "Aether-U", "Aether-Z",
            "Auger", "AuthProbe", "Bastion", "Dredge", "Forge",
            "Fracture", "Isotope", "OSift", "pAIn", "Quill",
            "Scattershot3", "Siphon", "Trawler",
        ],
        "database":  "SQLite (findings.db) — FindingsDB server-side pagination, full-text search",
    },
    {
        "name":        "Dredge",
        "repo":        "Dredge",
        "github_url":  "https://github.com/pr0t31n/Dredge.git",
        "description": "Windows endpoint security auditor: PowerShell collector (26 modules) + Python analyzer (19 modules, 150+ findings). CIS Benchmarks, DISA STIGs, NIST mapping. Import, remote WinRM, and Crucible managed modes.",
        "short_desc":  "Endpoint Security",
        "port":        None,
        "flask":       False,
        "main_file":   "dredge.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Drift",
        "repo":        "Drift",
        "github_url":  "https://github.com/pr0t31n/Drift.git",
        "description": "Configuration drift detector — compares current system state against baseline snapshots and flags deviations.",
        "short_desc":  "Config Drift",
        "port":        None,
        "flask":       False,
        "main_file":   "drift.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Forge",
        "repo":        "Forge",
        "github_url":  "https://github.com/pr0t31n/Forge.git",
        "description": "Cloud control-plane auditor — 170+ checks across 35 AWS services (S3, EC2, IAM, VPC, RDS, Lambda, CloudTrail, KMS, EKS, Bedrock, SageMaker, CloudFront, GuardDuty, Security Hub) + Azure (Storage, Key Vault, VMs, OpenAI) + GCP (GCS, Compute, IAM, Vertex AI). Read-only assessment via aws/az/gcloud CLIs.",
        "short_desc":  "Cloud Audit",
        "port":        None,
        "flask":       False,
        "main_file":   "forge.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Fracture",
        "repo":        "Fracture",
        "github_url":  "https://github.com/pr0t31n/Fracture.git",
        "description": "Multi-phase web application pentest orchestrator — 28 phases covering all OWASP Top 10 2025 categories: injection, auth bypass, IDOR, SSRF, CSRF, BFLA, GraphQL, deserialization, business logic, request smuggling, mass assignment, AI/ML service detection + pAIn handoff. 23 auth methods. CVE correlation via OSV/NVD.",
        "short_desc":  "WebApp",
        "port":        None,
        "flask":       False,
        "main_file":   "fracture.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
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
        "requires":    ["Gearbox", "Shared"],
        "database":  "SQLite (findings.db) — reads Gearbox FindingsDB",
    },
    {
        "name":        "Gearbox",
        "repo":        "Gearbox",
        "github_url":  "https://github.com/pr0t31n/Gearbox.git",
        "description": "Hardware security assessment CLI — 55 testing modules across 15 interface types (Ethernet, USB, HDMI, serial, CAN bus, RFID, ZigBee, Bluetooth, Wi-Fi, Sub-GHz, UWB, smart card, Z-Wave, LoRa, network tap). Orchestrates nmap, Nessus, sslscan, nuclei, Responder, and 20+ tools into structured JSONL + SQLite output. Managed by Gauntlet.",
        "short_desc":  "Hardware / IoT",
        "port":        None,
        "flask":       False,
        "main_file":   "gearbox.py",
        "service":     None,
        "requires":    ["Gauntlet", "Shared"],
        "database":  "SQLite (gearbox.db + findings.db) — device data, findings, FindingsDB",
    },
    {
        "name":        "Isotope",
        "repo":        "Isotope",
        "github_url":  "https://github.com/pr0t31n/Isotope.git",
        "description": "Universal file analysis tool: identification, entropy/encryption detection, string extraction, code analysis, metadata extraction, binary/executable analysis, secrets scanning, hashing with IOC lookup, and recursive archive decomposition.",
        "short_desc":  "File Analysis",
        "port":        None,
        "flask":       False,
        "main_file":   "isotope.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "LandNav",
        "repo":        "LandNav",
        "github_url":  "https://github.com/pr0t31n/LandNav.git",
        "description": "Automated rogue device network bootstrapper for Raspberry Pi drop devices. Transparent bridge, parallel C2 egress (chisel, iodine, meterpreter), passive recon, Responder analyzer mode, handler prep, and systemd service with external watchdog.",
        "short_desc":  "Drop Device",
        "port":        None,
        "flask":       False,
        "main_file":   "landnav.py",
        "service":     "pr0t31n-landnav",
        "requires":    [],
        "optional_deps": [
            {"name": "Mole-Client", "prompt": "Install Mole-Client for traffic tunnelling (routes outbound traffic through chisel)?"},
        ],
        "database":  None,
    },
    {
        "name":        "Mole",
        "repo":        "Mole",
        "github_url":  "https://github.com/pr0t31n/Mole.git",
        "description": "Traffic tunnelling toolkit — transparent SOCKS5 + DNS proxy for routing traffic through chisel or other upstream proxies. Client + Server components.",
        "short_desc":  "Traffic Tunnel",
        "port":        None,
        "flask":       False,
        "main_file":   "main.go",
        "service":     None,
        "requires":    ["Mole-Client", "Mole-Server"],
        "is_group":    True,
        "database":  None,
    },
    {
        "name":        "Mole-Client",
        "repo":        "Mole-Client",
        "github_url":  "https://github.com/pr0t31n/Mole-Client.git",
        "description": "Transparent SOCKS5 + DNS proxy client — iptables REDIRECT intercepts outbound traffic and tunnels it through a SOCKS upstream (e.g. chisel). Pre-built binaries for Linux amd64/arm64, macOS, Windows.",
        "short_desc":  "Traffic Proxy Client",
        "port":        None,
        "flask":       False,
        "main_file":   "mole.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Mole-Server",
        "repo":        "Mole-Server",
        "github_url":  "https://github.com/pr0t31n/Mole-Server.git",
        "description": "SOCKS5 + DNS proxy server daemon (moled) — receives tunnelled traffic from Mole-Client and forwards to the internet. Pre-built binaries for Linux amd64/arm64.",
        "short_desc":  "Traffic Proxy Server",
        "port":        None,
        "flask":       False,
        "main_file":   "Mole-Server-Install.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Nexus",
        "repo":        "Nexus",
        "github_url":  "https://github.com/pr0t31n/Nexus.git",
        "description": "Fleet management hub — fleet discovery, 20 fabric adapters, SSH terminal, Arsenal bridge, enrollment/dispatch, audit trail, custom third-party service monitoring (TCP/API/webhook).",
        "short_desc":  "Fleet Management Hub",
        "port":        6000,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-nexus",
        "requires":    [],
        "database":  "SQLite — fleet topology, custom services, encrypted credentials, audit log",
    },
    {
        "name":        "OSift",
        "repo":        "OSift",
        "github_url":  "https://github.com/pr0t31n/OSift.git",
        "description": "Comprehensive OSINT framework — 22-stage recon pipeline: DNS, subdomain discovery (parallel PD waves), breach intelligence, GitHub secret detection, dnstwist typosquatting, VirusTotal reputation, Azure tenant recon, people dorking, Shodan CLI, 30+ tools, 16 API integrations.",
        "short_desc":  "OSINT",
        "port":        None,
        "flask":       False,
        "main_file":   "osift.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "pAIn",
        "repo":        "pAIn",
        "github_url":  "https://github.com/pr0t31n/pAIn.git",
        "description": "AI security red-team tool — file-based prompt injection (12 formats), nuclei AI templates (~148), NVIDIA garak jailbreak testing (666+ probes), web app and API target support, whisper/transcription mode, OWASP LLM01:2025.",
        "short_desc":  "AI Red-Team",
        "port":        None,
        "flask":       False,
        "main_file":   "pain.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
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
        "database":  None,
    },
    {
        "name":        "Polaris",
        "repo":        "Polaris",
        "github_url":  "https://github.com/pr0t31n/Polaris.git",
        "description": "C2 server manager and real-time connection dashboard for LandNav. Manages chisel server, iodined, and msfconsole as independent processes. Flask + SocketIO dashboard on port 5900.",
        "short_desc":  "C2 Server Mgr",
        "port":        5900,
        "flask":       True,
        "main_file":   "app.py",
        "service":     "pr0t31n-polaris",
        "requires":    ["LandNav"],
        "database":  None,
    },
    {
        "name":        "Quill",
        "repo":        "Quill",
        "github_url":  "https://github.com/pr0t31n/Quill.git",
        "description": "Mobile application security platform — static analysis (iOS IPA + Android APK), dynamic analysis (MITM traffic capture + Frida instrumentation), secret scanning, and API endpoint discovery with auto-queue handoff to Fracture/Siphon.",
        "short_desc":  "Mobile Security",
        "port":        None,
        "flask":       False,
        "main_file":   "quill.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
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
        "database":  None,
    },
    {
        "name":        "Scattershot3",
        "repo":        "Scattershot3",
        "github_url":  "https://github.com/pr0t31n/Scattershot3.git",
        "description": "Network pentest recon — 20-phase pipeline: host discovery, port scanning, AI/ML service fingerprinting, TLS analysis, vulnerability scanning (nuclei), exploit lookup (searchsploit/Sploitus/Metasploit/CVE correlation), database attacks, credential brute-force, web enumeration, LLMNR poisoning, SMB/SNMP/AD enumeration, DAST (ZAP/sqlmap), and O365 enumeration.",
        "short_desc":  "Network",
        "port":        None,
        "flask":       False,
        "main_file":   "scattershot3.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Shared",
        "repo":        "shared",
        "github_url":  "https://github.com/pr0t31n/shared.git",
        "description": "Shared Python modules for the pr0t31n toolkit — universal output schema, event logging, tool runner, HTTP evidence capture, false-positive management, findings catalogue tooling, and engagement directory helpers.",
        "short_desc":  "Shared Modules",
        "port":        None,
        "flask":       False,
        "main_file":   "pr0t31n_schema.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
    {
        "name":        "Siphon",
        "repo":        "Siphon",
        "github_url":  "https://github.com/pr0t31n/Siphon.git",
        "description": "Multi-phase API pentest orchestrator — 25-phase OWASP API Security Top 10 2023 coverage: discovery + chaining, injection, JWT attacks, BOLA, BFLA, mass assignment, SSRF, GraphQL, business flow abuse, rate limiting, SOAP/WS, verb tampering, WebSocket/SSE/gRPC detection, and multi-credential access matrix.",
        "short_desc":  "API",
        "port":        None,
        "flask":       False,
        "main_file":   "siphon.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Trawler",
        "repo":        "Trawler",
        "github_url":  "https://github.com/pr0t31n/Trawler.git",
        "description": "Passive pcap-based traffic analyser — per-target external + internal risk surfacing. Auto-triggers after Scattershot3 scope completion. Emits findings into the universal schema.",
        "short_desc":  "PCAP Analyser",
        "port":        None,
        "flask":       False,
        "main_file":   "trawler.py",
        "service":     None,
        "requires":    [],
        "database":  "SQLite (findings.db) — FindingsDB dual-write",
    },
    {
        "name":        "Vault",
        "repo":        "Vault",
        "github_url":  "https://github.com/pr0t31n/Vault.git",
        "description": "Active Directory credential-test orchestrator: wraps impacket-secretsdump (DCSync/SAM), impacket-GetUserSPNs (Kerberoasting), impacket-GetNPUsers (AS-REP roasting), and SMB-signing inspection.",
        "short_desc":  "AD Cred Test",
        "port":        None,
        "flask":       False,
        "main_file":   "vault.py",
        "service":     None,
        "requires":    [],
        "database":  None,
    },
]

APP_INDEX: dict[str, dict] = {a["name"]: a for a in APPS}


def _build_display_items() -> list[dict]:
    """
    Build the ordered display list for the curses menu.

    Layout:
      • ALL top-level entries (parents AND standalones) sorted alphabetically
        together — no grouping by type.
      • Each parent's children indented below it, sorted alphabetically.
        Children come from two sources:
        - ``requires`` (install dependencies, minus _NO_NEST)
        - ``managed`` (display-only children — not install dependencies)
    """
    _NO_NEST = {"Shared", "Arsenal"}
    nested: set[str] = set()
    for a in APPS:
        if a["flask"] or a.get("is_group"):
            for req in a.get("requires", []):
                if req not in _NO_NEST:
                    nested.add(req)
            for m in a.get("managed", []):
                nested.add(m)

    parent_set = {
        a["name"] for a in APPS
        if (a["flask"] or a.get("is_group")) and a["name"] not in nested
    }

    top_level = sorted(
        (a for a in APPS if a["name"] not in nested),
        key=lambda a: a["name"].lower(),
    )

    items: list[dict] = []
    for app in top_level:
        items.append({"app": app, "indent": 0})
        if app["name"] in parent_set:
            child_names = set()
            for req in app.get("requires", []):
                if req not in _NO_NEST:
                    child_names.add(req)
            for m in app.get("managed", []):
                child_names.add(m)
            for child_name in sorted(child_names):
                child = APP_INDEX.get(child_name)
                if child:
                    items.append({"app": child, "indent": 1})

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
# System prerequisite check — runs before anything else
# ─────────────────────────────────────────────────────────────────────────────
_PREREQS_CRITICAL = [
    # (binary, version_flag, apt_pkg, brew_pkg, dnf_pkg, description)
    ("git",     "--version",  "git",           "git",    "git",           "version control — required for all installs"),
    ("curl",    "--version",  "curl",           "curl",   "curl",          "HTTP client — used by installers"),
    ("pip3",    "--version",  "python3-pip",    None,     "python3-pip",   "Python package manager"),
    ("openssl", "version",    "openssl",        "openssl","openssl",       "TLS/crypto — required by Flask apps"),
]
_PREREQS_BUILD = [
    ("go",      "version",    "golang",         "go",     "golang",        "Go compiler — needed by Mole, Gearbox deps"),
    ("gcc",     "--version",  "gcc",            None,     "gcc",           "C compiler — needed by some pip packages"),
    ("make",    "--version",  "make",           None,     "make",          "build tool — needed by some native deps"),
    ("gem",     "--version",  "ruby",           "ruby",   "ruby",          "Ruby gems — needed by WPScan, BundleAudit"),
    ("npm",     "--version",  "npm",            "node",   "npm",           "Node.js — needed by some web tools"),
    ("cargo",   "--version",  "cargo",          "rust",   "cargo",         "Rust — needed by RustScan, feroxbuster"),
]

def _get_version(binary: str, flag: str) -> "str | None":
    path = shutil.which(binary)
    if not path:
        return None
    try:
        r = subprocess.run([path, flag], capture_output=True, text=True, timeout=10)
        out = (r.stdout or r.stderr or "").strip()
        m = re.search(r'(\d+\.\d+[\w.+-]*)', out)
        return m.group(1) if m else "installed"
    except Exception:
        return "installed"

def _get_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("arm"):
        return "armv7"
    return machine

def _install_prereq(pkg_apt: "str|None", pkg_brew: "str|None", pkg_dnf: "str|None",
                    os_type: str, binary: str) -> "tuple[bool, str]":
    """Install a prerequisite. Returns (success, error_detail)."""
    def _err(r: "subprocess.CompletedProcess") -> str:
        out = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
        lines = [l for l in out.splitlines() if l.strip()]
        return "\n".join(lines[-6:]) if lines else f"exit code {r.returncode}"

    if os_type == "macos":
        brew = shutil.which("brew")
        if not brew or not pkg_brew:
            return False, "no package manager available (brew not found)" if not brew else "no brew package defined"
        if binary == "pip3":
            r = subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and shutil.which("pip3"):
                return True, ""
            return False, _err(r)
        r = subprocess.run([brew, "install", pkg_brew],
                           capture_output=True, text=True, timeout=300)
        return (r.returncode == 0, "" if r.returncode == 0 else _err(r))
    elif os_type in ("kali", "ubuntu", "linux"):
        pm = shutil.which("apt-get") or shutil.which("apt")
        if not pm or not pkg_apt:
            return False, "no package manager available" if not pm else "no apt package defined"
        upd = subprocess.run([pm, "update", "-qq"], capture_output=True, text=True, timeout=120)
        if upd.returncode != 0:
            return False, f"apt update failed:\n{_err(upd)}"
        r = subprocess.run([pm, "install", "-y", pkg_apt],
                           capture_output=True, text=True, timeout=300)
        return (r.returncode == 0, "" if r.returncode == 0 else _err(r))
    elif os_type == "rhel":
        pm = shutil.which("dnf") or shutil.which("yum")
        if not pm or not pkg_dnf:
            return False, "no package manager available" if not pm else "no dnf/yum package defined"
        r = subprocess.run([pm, "install", "-y", pkg_dnf],
                           capture_output=True, text=True, timeout=300)
        return (r.returncode == 0, "" if r.returncode == 0 else _err(r))
    elif os_type == "suse":
        pm = shutil.which("zypper")
        if not pm or not pkg_dnf:
            return False, "no package manager available" if not pm else "no zypper package defined"
        r = subprocess.run([pm, "install", "-y", pkg_dnf],
                           capture_output=True, text=True, timeout=300)
        return (r.returncode == 0, "" if r.returncode == 0 else _err(r))
    return False, f"unsupported OS type: {os_type}"

def check_prerequisites() -> None:
    os_type = detect_os()
    arch = _get_arch()
    print(f"\n{_BLD}── System Prerequisites ─────────────────────────────────────{_R}")
    print(f"  OS: {os_type}   Arch: {arch}   Python: {platform.python_version()}\n")

    missing_critical: list[tuple] = []
    missing_build: list[tuple] = []

    for group, label, missing_list in [
        (_PREREQS_CRITICAL, "Core (required)", missing_critical),
        (_PREREQS_BUILD, "Build tools (optional)", missing_build),
    ]:
        print(f"  {_BLD}{label}:{_R}")
        for binary, flag, pkg_apt, pkg_brew, pkg_dnf, desc in group:
            ver = _get_version(binary, flag)
            path = shutil.which(binary) or ""
            if ver:
                print(f"    {_G}✓{_R} {binary:<10} {_DIM}{ver:<20}{_R} {path}")
            else:
                print(f"    {_RD}✗{_R} {binary:<10} {_RD}not found{_R}             {_DIM}{desc}{_R}")
                missing_list.append((binary, flag, pkg_apt, pkg_brew, pkg_dnf, desc))
        print()

    if not missing_critical and not missing_build:
        ok("All prerequisites satisfied.\n")
        return

    # Handle critical missing
    if missing_critical:
        err(f"{len(missing_critical)} critical prerequisite(s) missing — Arsenal cannot install tools without them.")
        print()
        for binary, flag, pkg_apt, pkg_brew, pkg_dnf, desc in missing_critical:
            info(f"Installing {binary} ({desc})…")
            ok_flag, detail = _install_prereq(pkg_apt, pkg_brew, pkg_dnf, os_type, binary)
            if ok_flag:
                ver = _get_version(binary, flag)
                ok(f"{binary} installed → {ver}")
            else:
                err(f"Could not install {binary} automatically.")
                if detail:
                    for dl in detail.splitlines():
                        print(f"    {_DIM}{dl}{_R}")
                if os_type == "macos":
                    hint = f"brew install {pkg_brew}" if pkg_brew else f"Install {binary} manually"
                elif os_type in ("kali", "ubuntu", "linux"):
                    hint = f"apt-get install -y {pkg_apt}" if pkg_apt else f"Install {binary} manually"
                else:
                    hint = f"Install {binary} via your package manager"
                print(f"    {_Y}Manual fix:{_R}  {hint}")
        print()
        # Re-check critical after install attempts
        still_missing = [b for b, *_ in missing_critical if not shutil.which(b)]
        if still_missing:
            err(f"Cannot continue — missing: {', '.join(still_missing)}")
            err("Fix the above and re-run Arsenal.")
            sys.exit(1)
        ok("All critical prerequisites now satisfied.\n")

    # Handle optional build tools
    if missing_build:
        warn(f"{len(missing_build)} build tool(s) not found — some tools may fail to install.")
        resp = _clean_input(f"  {_BLD}Install missing build tools now? [Y/n]{_R} ").strip().lower()
        if resp in ("", "y", "yes"):
            for binary, flag, pkg_apt, pkg_brew, pkg_dnf, desc in missing_build:
                info(f"Installing {binary}…")
                ok_flag, detail = _install_prereq(pkg_apt, pkg_brew, pkg_dnf, os_type, binary)
                if ok_flag:
                    ver = _get_version(binary, flag)
                    ok(f"{binary} installed → {ver}")
                else:
                    warn(f"Could not install {binary} — some tools needing it may fail.")
                    if detail:
                        for dl in detail.splitlines():
                            print(f"    {_DIM}{dl}{_R}")
        else:
            info("Skipping build tools — you can install them later.\n")


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

    # 2 — Credential check (only when --token was supplied)
    token = _GH_CREDS.get("token", "")
    if not token:
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
    """Read the VERSION file from GitHub.

    Strategy (in order):
      1. Contents API (api.github.com) — no CDN cache, works with or without
         a token (unauthenticated gets 60 req/hr which is enough for status).
      2. raw.githubusercontent.com — CDN-cached up to 5 min; used as fallback
         only when the API is unreachable or rate-limited.
    """
    repo = app["repo"]
    token = _GH_CREDS.get("token", "")
    _cb = str(int(time.time() * 1000))

    for branch in ("main", "master"):
        try:
            hdrs = _gh_api_headers() if token else {
                "User-Agent": f"Arsenal/{VERSION}",
                "Accept": "application/vnd.github.v3+json",
            }
            hdrs["Cache-Control"] = "no-cache, no-store"
            hdrs["Pragma"] = "no-cache"
            hdrs.pop("If-None-Match", None)
            req = urllib.request.Request(
                f"https://api.github.com/repos/pr0t31n/{repo}/contents/VERSION?ref={branch}&_={_cb}",
                headers=hdrs,
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
                content = data.get("content", "")
                if content and data.get("encoding") == "base64":
                    ver = base64.b64decode(content).decode("utf-8", errors="replace").strip()
                    if ver:
                        return ver
        except Exception:
            pass

    for branch in ("main", "master"):
        try:
            req = urllib.request.Request(
                f"https://raw.githubusercontent.com/pr0t31n/{repo}/{branch}/VERSION?_={_cb}",
                headers={"User-Agent": f"Arsenal/{VERSION}",
                         "Cache-Control": "no-cache, no-store",
                         "Pragma": "no-cache"},
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                ver = r.read().decode("utf-8", errors="replace").strip()
                if ver:
                    return ver
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
# Linux systemd unit management
# ─────────────────────────────────────────────────────────────────────────────
def _write_systemd_unit(app: dict) -> Optional[Path]:
    """
    Write (or overwrite) the systemd service unit for a Flask app on Linux.
    Returns the unit path on success, None if not Linux or not a Flask app.
    """
    if SYSTEM != "Linux" or not app.get("flask"):
        return None
    svc = app.get("service")
    if not svc:
        return None

    python_bin  = shutil.which("python3") or sys.executable
    install_dir = _install_dir(app)
    main_script = install_dir / app["main_file"]
    unit_path   = Path(f"/etc/systemd/system/{svc}.service")

    content = f"""[Unit]
Description={app.get('description', app['name'])}
After=network.target

[Service]
Type=simple
WorkingDirectory={install_dir}
ExecStart={python_bin} {main_script}
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/{svc}.log
StandardError=append:/var/log/{svc}.err

[Install]
WantedBy=multi-user.target
"""
    try:
        unit_path.write_text(content)
        unit_path.chmod(0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=10)
        subprocess.run(["systemctl", "enable", svc], check=False, timeout=10)
        ok(f"Wrote systemd unit: {unit_path}  →  {main_script}")
        return unit_path
    except Exception as exc:
        warn(f"Could not write systemd unit for {app['name']}: {exc}")
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
    # same files on disk). Return the bare version string (no source-of-
    # truth suffix — that breaks column-width alignment in the curses
    # status table and was operator-flagged as noise).
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
                    return lv
        elif SYSTEM == "Darwin":
            label = f"com.pr0t31n.{app['name'].lower()}"
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0:
                lv = get_local_version(app)
                if lv:
                    return lv
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
            _write_systemd_unit(app)
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
            and _version_gt(github_ver, local_ver)
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


def _update_app(app: dict, installer_args: "list[str] | None" = None) -> None:
    if app["name"] not in ("Arsenal", "Shared"):
        _ensure_shared_current()
    install_dir = _install_dir(app)
    info(f"Updating {app['name']}…")
    r = _git_pull_with_pycache_retry(install_dir, _git_env())
    if r.returncode == 0:
        rc = _run_install_py(install_dir, extra_args=installer_args)
        if rc == 0:
            ok(f"{app['name']} updated.")
        else:
            warn(f"{app['name']} updated (install.py returned {rc}).")
    else:
        err(f"git pull failed for {app['name']}: {r.stderr.strip()[:120]}")
        _FAILURES.append({
            "phase": "git",
            "name":  app["name"],
            "description": "git pull --ff-only failed",
            "success": False,
            "stderr": r.stderr.strip()[:200],
            "error": "git pull failed",
        })
    _write_plist(app)
    _write_systemd_unit(app)
    _handle_optional_deps(app, is_update=True)


# ─────────────────────────────────────────────────────────────────────────────
# Install / Uninstall
# ─────────────────────────────────────────────────────────────────────────────
def _run_install_py(install_dir: Path, extra_args: "list[str] | None" = None) -> int:
    installer = install_dir / "install.py"
    if not installer.exists():
        warn(f"No install.py found in {install_dir.name} — skipping post-install.")
        return 0
    info(f"Running install.py in {install_dir.name}…")
    cmd = [sys.executable, "-u", str(installer)] + (extra_args or [])
    errors: list[str] = []
    _ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    proc = subprocess.Popen(cmd, cwd=str(install_dir),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace")
        sys.stdout.write(line)
        sys.stdout.flush()
        plain = _ansi_re.sub("", line).strip()
        if plain.startswith("[✗]"):
            errors.append(plain)
    proc.wait()
    if proc.returncode != 0:
        _FAILURES.append({
            "phase": "install.py",
            "name":  install_dir.name,
            "description": f"install.py exited with code {proc.returncode}",
            "success": False,
            "stderr": "\n".join(errors[-10:]),
            "error": f"install.py returned exit code {proc.returncode}",
        })
    for e in errors:
        _FAILURES.append({
            "phase": "install.py",
            "name":  install_dir.name,
            "description": e[4:].strip() if len(e) > 4 else e,
            "success": False,
            "stderr": "",
            "error": e,
        })
    return proc.returncode


def _ensure_shared_current() -> None:
    """Pull latest shared modules before installing/upgrading a dependent tool."""
    if "Shared" not in APP_INDEX:
        return
    shared = APP_INDEX["Shared"]
    shared_dir = _install_dir(shared)
    git_env = _git_env()
    if shared_dir.is_dir() and (shared_dir / ".git").is_dir():
        r = _git_pull_with_pycache_retry(shared_dir, git_env)
        if r.returncode == 0 and "Already up to date" not in (r.stdout or ""):
            info("Shared modules updated.")
    elif not shared_dir.is_dir():
        info("Installing shared modules (dependency)…")
        shared_dir.parent.mkdir(parents=True, exist_ok=True)
        r = _git_run([
            "git", "clone", "--depth=1", _authed_url(shared["github_url"]),
            str(shared_dir),
        ], capture_output=True, text=True, env=git_env)
        if r.returncode == 0:
            ok("Shared modules installed.")


def _handle_optional_deps(app: dict, is_update: bool = False) -> None:
    """Check optional_deps for an app. On install: prompt if not present.
    On update: auto-update if already installed, skip if not."""
    for dep in app.get("optional_deps", []):
        dep_name = dep["name"]
        if dep_name not in APP_INDEX:
            continue
        dep_app = APP_INDEX[dep_name]
        dep_dir = _install_dir(dep_app)
        if dep_dir.is_dir():
            if is_update:
                info(f"Updating optional dependency {dep_name}…")
                _update_app(dep_app)
        else:
            if not is_update:
                try:
                    ans = input(f"\n{_BLD}{dep.get('prompt', f'Install optional dependency {dep_name}?')} [y/N]{_R} ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if ans == "y":
                    install_app(dep_app)


def install_app(app: dict) -> None:
    """Clone repo into INSTALL_BASE (or app install_path) and run install.py."""
    if app["name"] not in ("Arsenal", "Shared"):
        _ensure_shared_current()
    install_dir = _install_dir(app)
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    git_env     = _git_env()

    if install_dir.is_dir():
        info(f"{app['name']} already present — pulling latest…")
        _git_pull_with_pycache_retry(install_dir, git_env)
    else:
        info(f"Cloning {app['github_url']}  →  {install_dir}")
        r = _git_run([
            "git", "clone", "--depth=1", _authed_url(app["github_url"]), str(install_dir),
        ], capture_output=True, text=True, env=git_env)
        if r.returncode != 0:
            err(f"Clone failed for {app['name']}.")
            return
        _ensure_filemode_off(install_dir)

    _run_install_py(install_dir)
    plist = _write_plist(app)
    _write_systemd_unit(app)
    if plist and app.get("service") and SYSTEM == "Darwin":
        subprocess.run(["launchctl", "load", "-w", str(plist)],
                       check=False, timeout=10)
        ok(f"{app['name']} service loaded via launchctl.")
    ok(f"{app['name']} installed.")
    _handle_optional_deps(app, is_update=False)


def _find_tool_pids(app: dict) -> list[int]:
    """Find PIDs of running processes belonging to a tool."""
    pids: set[int] = set()
    install_dir = str(_install_dir(app))
    port = app.get("port")

    if port:
        try:
            r = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        except Exception:
            pass

    try:
        r = subprocess.run(
            ["pgrep", "-f", install_dir],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != os.getpid():
                    pids.add(pid)
    except Exception:
        pass

    return sorted(pids)


def _kill_tool_processes(app: dict) -> int:
    """Find and kill any running processes for a tool. Returns kill count."""
    pids = _find_tool_pids(app)
    if not pids:
        return 0

    info(f"Found {len(pids)} running process(es) for {app['name']}: {pids}")
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            info(f"  Sent SIGTERM to PID {pid}")
        except ProcessLookupError:
            continue
        except PermissionError:
            warn(f"  No permission to signal PID {pid}")
            continue
        killed += 1

    if killed:
        time.sleep(3)
        for pid in pids:
            try:
                os.kill(pid, 0)
                info(f"  PID {pid} still alive — sending SIGKILL")
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass

    return killed


def _uninstall_dep(dep: dict) -> bool:
    """Remove a single dependency using the method it was installed with."""
    name = dep["name"]
    method = dep["method"]
    pm = dep.get("pkg_manager", method)

    try:
        if method == "system":
            if pm in ("apt", "apt-get") or shutil.which("apt-get"):
                r = subprocess.run(
                    ["apt-get", "remove", "-y", name],
                    capture_output=True, text=True, timeout=120,
                )
                return r.returncode == 0
            elif pm == "dnf" or shutil.which("dnf"):
                r = subprocess.run(
                    ["dnf", "remove", "-y", name],
                    capture_output=True, text=True, timeout=120,
                )
                return r.returncode == 0
            elif pm == "zypper" or shutil.which("zypper"):
                r = subprocess.run(
                    ["zypper", "remove", "-y", name],
                    capture_output=True, text=True, timeout=120,
                )
                return r.returncode == 0
        elif method == "pip":
            pkg_name = name.split(">=")[0].split("==")[0].split("[")[0].strip()
            r = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y",
                 "--break-system-packages", pkg_name],
                capture_output=True, text=True, timeout=120,
            )
            return r.returncode == 0
        elif method == "go":
            binary = Path("/usr/local/bin") / name
            if binary.is_file() or binary.is_symlink():
                binary.unlink()
                return True
            return False
        elif method in ("brew", "brew-cask"):
            brew = shutil.which("brew")
            if brew:
                r = subprocess.run(
                    [brew, "uninstall", name],
                    capture_output=True, text=True, timeout=120,
                )
                return r.returncode == 0
        elif method == "git":
            dep_path = Path(name) if "/" in name else None
            if dep_path and dep_path.is_dir():
                shutil.rmtree(dep_path)
                return True
    except Exception as exc:
        warn(f"  Failed to remove {name}: {exc}")
    return False


def _prompt_dep_removal(app: dict) -> None:
    """After uninstalling a tool, offer to remove its Arsenal-installed deps."""
    tool_name = app["name"]
    deps = _get_tool_deps(tool_name)
    if not deps:
        _remove_tool_deps(tool_name)
        return

    shared_set = dict(_find_shared_deps(tool_name))
    removable = [d for d in deps if d["name"] not in shared_set]
    shared    = [d for d in deps if d["name"] in shared_set]

    if shared:
        info(f"Shared dependencies (kept — used by other tools):")
        for d in shared:
            others = ", ".join(shared_set[d["name"]])
            print(f"    {_Y}⤷{_R}  {d['name']}  ({d['method']})  "
                  f"— also used by: {others}", flush=True)

    if not removable:
        info("No unique dependencies to remove.")
        _remove_tool_deps(tool_name)
        return

    print(f"\n  {_BLD}Arsenal-installed dependencies for {tool_name} "
          f"({len(removable)} removable):{_R}")
    for d in removable:
        print(f"    {_RD}✗{_R}  {d['name']}  ({d['method']} via {d.get('pkg_manager', d['method'])})")

    try:
        ans = input(f"\n  {_BLD}Remove these {len(removable)} "
                    f"dependencies? [y/N]{_R} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        ans = "n"

    if ans == "y":
        removed = 0
        for d in removable:
            ok_flag = _uninstall_dep(d)
            sym = f"{_G}[+]{_R}" if ok_flag else f"{_RD}[!]{_R}"
            status = "removed" if ok_flag else "FAILED"
            print(f"    {sym}  {d['name']}  — {status}", flush=True)
            if ok_flag:
                removed += 1
        ok(f"Removed {removed}/{len(removable)} dependencies.")
    else:
        info("Dependencies kept.")

    _remove_tool_deps(tool_name)


def uninstall_app(app: dict) -> None:
    """Stop service, kill processes, remove symlinks, delete install directory,
    and optionally remove Arsenal-installed dependencies."""
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

    # ── Force-kill surviving processes ────────────────────────────────────────
    killed = _kill_tool_processes(app)
    if killed:
        ok(f"Killed {killed} process(es) for {app['name']}.")

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

    # ── Dependency cleanup ────────────────────────────────────────────────────
    _prompt_dep_removal(app)


# ─────────────────────────────────────────────────────────────────────────────
# Dependency resolution
# ─────────────────────────────────────────────────────────────────────────────
def resolve_dependencies(
    to_install:   list[str],
    to_uninstall: list[str],
    status:       dict[str, dict],
) -> tuple[list[str], list[str]]:
    """
    Ensure each Flask app being installed has AT LEAST ONE of its listed
    `requires` tools either already installed or queued in this install
    batch.  Older behaviour auto-dragged in EVERY required tool, which
    over-installed Flask apps that only need a subset (e.g. the
    Aether family — most users want one or two, not all four).

    If none of a Flask app's required tools are installed or queued, the
    first entry in its `requires` list is auto-added so the Flask app
    has something to drive.  Anything beyond that is left to the user.

    Warn if uninstalling something that another installed app depends on.
    Returns (ordered_install_list, uninstall_list).
    """
    install_set   = set(to_install)
    uninstall_set = set(to_uninstall)
    added: list[str] = []

    for name in list(to_install):
        deps = APP_INDEX[name].get("requires", [])
        if not deps:
            continue
        # "Satisfied" = at least one of the required tools is already
        # installed on the system OR was selected for install in this batch.
        satisfied = any(
            (d in install_set) or status.get(d, {}).get("installed")
            for d in deps
        )
        if not satisfied:
            # Auto-add the first required tool so the Flask app isn't
            # installed against zero supported backends.
            first = deps[0]
            install_set.add(first)
            added.append(first)

    if added:
        print()
        warn("Auto-adding minimum required dependency (one per Flask app):")
        for a in added:
            print(f"    {_Y}+{_R}  {a}")
        print("    (install additional supported tools later from the menu)")

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
    stdscr.timeout(1500)  # 1.5 s — repaint covers SSH channel noise

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

        if max_y < 10 or max_x < 28:
            stdscr.addstr(0, 0, "Terminal too small.")
            stdscr.refresh()
            stdscr.getch()
            return None

        _compact = max_x < 60

        # ── shared state for keyboard handlers (both modes) ──────────────
        checked_names = [a["name"] for a in APPS if desired[a["name"]]]
        can_install = any(not status[n]["installed"] for n in checked_names)
        can_uninstall = any(status[n]["installed"] for n in checked_names)
        can_upgrade = any(
            status[n]["installed"]
            and _version_gt(status[n].get("github_ver") or "",
                            status[n].get("local_ver") or "")
            for n in checked_names)
        can_push = any(
            status[n]["installed"]
            and _version_gt(status[n].get("local_ver") or "",
                            status[n].get("github_ver") or "")
            for n in checked_names)
        can_restart = any(
            item["app"]["flask"]
            and status[item["app"]["name"]]["installed"]
            and desired[item["app"]["name"]]
            for item in _DISPLAY_ITEMS)

        # ══════════════════════════════════════════════════════════════════
        #  COMPACT MODE — mobile-friendly layout for narrow terminals
        # ══════════════════════════════════════════════════════════════════
        if _compact:
            # In compact mode cursor wraps within app rows only — actions
            # are triggered exclusively via keyboard shortcuts.
            if cursor >= N:
                cursor = N - 1

            sep_c = "─" * min(max_x - 2, 60)

            # ── header ───────────────────────────────────────────────────
            _hdr_c = f" Arsenal v{VERSION}"
            stdscr.addstr(0, 0, _hdr_c[:safe_x],
                          curses.color_pair(_CP_HEADER) | curses.A_BOLD)
            try:
                stdscr.addstr(1, 0, sep_c[:safe_x],
                              curses.color_pair(_CP_HEADER))
            except curses.error:
                pass

            # ── app rows ─────────────────────────────────────────────────
            # Layout: 2 header + viewport + 1 sep + 4 detail + 1 sep + 1 hints
            _detail_h = min(4, max(2, max_y - 6))
            _compact_footer = 1 + _detail_h + 1 + 1  # sep+detail+sep+hints
            viewport = max(1, max_y - 2 - _compact_footer)

            if cursor < scroll:
                scroll = cursor
            elif cursor >= scroll + viewport:
                scroll = cursor - viewport + 1
            scroll = max(0, min(scroll, max(0, N - viewport)))

            _c_col_chk = 1
            _c_col_dot = 5

            for vi in range(viewport):
                row_idx = vi + scroll
                screen_y = vi + 2
                if screen_y >= max_y - _compact_footer:
                    break
                if row_idx >= N:
                    break

                _di   = _DISPLAY_ITEMS[row_idx]
                app   = _di["app"]
                name  = app["name"]
                st_c  = status[name]
                d     = desired[name]

                chk, chk_cp = ("[✓]", _CP_CHECKED) if d else ("[ ]", _CP_UNCKD)
                inst_m, inst_cp = ("●", _CP_CHECKED) if st_c["installed"] else ("○", _CP_UNCKD)
                _name_w = max(0, safe_x - 7)
                _nc = ("↳" + name) if _di["indent"] else name
                _nc = _nc[:_name_w]

                _row_c = f" {chk} {inst_m} {_nc}"
                is_on = (row_idx == cursor)
                _ra = (curses.color_pair(_CP_HILITE)
                       if is_on else curses.color_pair(_CP_NORMAL))
                try:
                    stdscr.addstr(screen_y, 0, _row_c[:safe_x], _ra)
                    _ca = (curses.color_pair(_CP_HILITE) | curses.A_BOLD
                           if is_on
                           else curses.color_pair(chk_cp) | curses.A_BOLD)
                    stdscr.addstr(screen_y, _c_col_chk, chk, _ca)
                    _ia = (curses.color_pair(_CP_HILITE) | curses.A_BOLD
                           if is_on
                           else curses.color_pair(inst_cp) | curses.A_BOLD)
                    stdscr.addstr(screen_y, _c_col_dot, inst_m, _ia)
                except curses.error:
                    pass

            # ── scrollbar ────────────────────────────────────────────────
            if N > viewport:
                _sb_h = viewport
                _sb_pos = int(scroll / max(1, N - viewport) * (_sb_h - 1))
                for bi in range(_sb_h):
                    _sc = "█" if bi == _sb_pos else "│"
                    try:
                        stdscr.addch(2 + bi, safe_x, _sc,
                                     curses.color_pair(_CP_HEADER))
                    except curses.error:
                        pass

            # ── detail pane ──────────────────────────────────────────────
            _dy = max_y - _compact_footer
            try:
                stdscr.addstr(_dy, 0, sep_c[:safe_x],
                              curses.color_pair(_CP_HEADER))
            except curses.error:
                pass

            if cursor < N:
                _da = _DISPLAY_ITEMS[cursor]["app"]
                _ds = status[_da["name"]]
                _dlv = _ds["local_ver"] or ""
                _dgv = _ds["github_ver"] or ""
                _drv = _ds.get("running_ver", "") or ""
                _dp = str(_da["port"]) if _da["port"] else "—"

                # Line 1: name + port
                _d1 = f" {_da['name']}"
                _d1_port = f":{_dp}"
                _d1_pad = max(0, safe_x - len(_d1) - len(_d1_port))
                _d1_full = _d1 + " " * _d1_pad + _d1_port
                try:
                    stdscr.addstr(_dy + 1, 0, _d1_full[:safe_x],
                                  curses.color_pair(_CP_NORMAL) | curses.A_BOLD)
                except curses.error:
                    pass

                # Line 2: versions with colour
                if _detail_h >= 2:
                    _lv_d = _dlv or "—"
                    _gv_d = _dgv or "?"
                    _rv_d = _drv or "—"
                    _v_line = f" I:{_lv_d}  G:{_gv_d}"
                    if _da["flask"]:
                        _v_line += f"  R:{_rv_d}"
                    try:
                        stdscr.addstr(_dy + 2, 0, _v_line[:safe_x],
                                      curses.color_pair(_CP_NORMAL))
                        # Colour installed version if stale
                        _lv_x = 3
                        _gv_x = 3 + len(_lv_d) + 4
                        if (_dlv and _dgv
                                and _dlv not in ("unknown",) and _dgv not in ("?",)):
                            if _version_gt(_dgv, _dlv) and _lv_x < safe_x:
                                stdscr.addstr(_dy + 2, _lv_x,
                                              _lv_d[:safe_x - _lv_x],
                                              curses.color_pair(_CP_VER_OLD))
                            elif _version_gt(_dlv, _dgv) and _gv_x < safe_x:
                                stdscr.addstr(_dy + 2, _gv_x,
                                              _gv_d[:safe_x - _gv_x],
                                              curses.color_pair(_CP_VER_OLD))
                        # Colour running version
                        if _da["flask"] and _drv:
                            _rv_x = len(_v_line) - len(_rv_d)
                            if _rv_x > 0 and _rv_x < safe_x:
                                if _drv != _dlv:
                                    stdscr.addstr(
                                        _dy + 2, _rv_x,
                                        _rv_d[:safe_x - _rv_x],
                                        curses.color_pair(_CP_UPGRADE_BTN))
                                else:
                                    stdscr.addstr(
                                        _dy + 2, _rv_x,
                                        _rv_d[:safe_x - _rv_x],
                                        curses.color_pair(_CP_CHECKED))
                    except curses.error:
                        pass

                # Line 3: description
                if _detail_h >= 3:
                    _desc_c = _da.get("short_desc", _da["description"])
                    _desc_c = " " + _desc_c[:safe_x - 1]
                    try:
                        stdscr.addstr(_dy + 3, 0, _desc_c[:safe_x],
                                      curses.color_pair(_CP_UNCKD))
                    except curses.error:
                        pass

                # Line 4: available actions
                if _detail_h >= 4:
                    _acts = []
                    if can_install:
                        _acts.append("i:Inst")
                    if can_uninstall:
                        _acts.append("x:Del")
                    if can_upgrade:
                        _acts.append("U:Upg")
                    if can_push:
                        _acts.append("p:Push")
                    if _da["flask"] and _ds["installed"]:
                        _acts.append("r:Rst")
                    _acts_line = " " + " ".join(_acts) if _acts else " (select tools)"
                    try:
                        stdscr.addstr(_dy + 4, 0, _acts_line[:safe_x],
                                      curses.color_pair(_CP_HEADER))
                    except curses.error:
                        pass

            # ── key hints ────────────────────────────────────────────────
            _hy = max_y - 1
            try:
                stdscr.addstr(_hy - 1, 0, sep_c[:safe_x],
                              curses.color_pair(_CP_HEADER))
            except curses.error:
                pass
            _hints_c = " ↑↓:nav SPC:sel f:ref q:quit"
            try:
                stdscr.addstr(_hy, 0, _hints_c[:safe_x],
                              curses.color_pair(_CP_HEADER))
            except curses.error:
                pass

        # ══════════════════════════════════════════════════════════════════
        #  FULL MODE — standard wide-terminal layout
        # ══════════════════════════════════════════════════════════════════
        else:
            if max_y < 12:
                stdscr.addstr(0, 0, "Terminal too small — resize and retry.")
                stdscr.refresh()
                stdscr.getch()
                return None

            # ── column header ────────────────────────────────────────────
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

            # ── visible app rows ─────────────────────────────────────────
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

            # ── scrollbar (right edge, app area only) ────────────────────
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

            # ── footer ───────────────────────────────────────────────────
            fy = max_y - 4
            sep_line = "  " + "─" * min(max_x - 4, 100)
            try:
                stdscr.addstr(fy, 0, sep_line[:safe_x], curses.color_pair(_CP_HEADER))
            except curses.error:
                pass

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
                       "Dependencies: Crucible→managed tools  Gauntlet→Gearbox  Polaris→LandNav")
                stdscr.addstr(fy + 3, 0, req[:safe_x],
                              curses.color_pair(_CP_UNCKD) | curses.A_DIM)
            except curses.error:
                pass

        stdscr.refresh()

        # ── keyboard input ────────────────────────────────────────────────────
        key = stdscr.getch()

        if key == -1:  # timeout — loop repaints screen, covering SSH noise
            continue

        _wrap = N if _compact else TOTAL

        if key in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % _wrap

        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % _wrap

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
                    _upg = [n for n in checked_names
                            if status[n]["installed"]
                            and status[n].get("needs_update")]
                    return {"action": "upgrade", "checked": _upg}
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
                _upg = [n for n in checked_names
                        if status[n]["installed"]
                        and status[n].get("needs_update")]
                return {"action": "upgrade", "checked": _upg}

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
            path = (item.get("path") or item.get("local_path")
                    or item.get("dest") or "")
            if isinstance(path, Path):
                path = str(path)
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
    Also populates _DEP_TOOL_MAP for dependency attribution tracking.
    """
    global _DEP_TOOL_MAP
    _DEP_TOOL_MAP = {}
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
                if dk:
                    _DEP_TOOL_MAP.setdefault((key, dk), set()).add(name)
                    if key == "GO_TOOLS" and len(norm) >= 2 and norm[1]:
                        _DEP_TOOL_MAP.setdefault((key, norm[1]), set()).add(name)
                    elif key == "GIT_TOOLS" and len(norm) >= 2 and norm[1]:
                        _DEP_TOOL_MAP.setdefault((key, norm[1]), set()).add(name)
                        short = Path(norm[1]).name
                        if short:
                            _DEP_TOOL_MAP.setdefault((key, short), set()).add(name)
                    if dk not in seen[key]:
                        seen[key].add(dk)
                        merged[key].append(norm)
        merged["OFFLINE_BUNDLE_URLS"].update(_extract_offline_bundles(d))
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Install log (populated by phase runners, consumed by report generator)
# ─────────────────────────────────────────────────────────────────────────────
_INSTALL_LOG: list = []   # list[dict]
_FAILURES: list = []      # list[dict] — collects failures across all paths

# ─────────────────────────────────────────────────────────────────────────────
# Dependency manifest — persistent record of what Arsenal installed per tool
# Stored OUTSIDE /opt/Arsenal so it survives directory deletion / re-pull.
# ─────────────────────────────────────────────────────────────────────────────
_DEP_MANIFEST_PATH = Path("/opt/pr0t31n/.arsenal/deps.json")

_DEP_TOOL_MAP: dict = {}

_PHASE_TO_PKG_KEYS: dict[str, set[str]] = {
    "system":    {"APT_PACKAGES", "DNF_PACKAGES", "ZYPPER_PACKAGES"},
    "pip":       {"PIP_PACKAGES"},
    "go":        {"GO_TOOLS"},
    "brew":      {"BREW_PACKAGES"},
    "brew-cask": {"BREW_CASK_PACKAGES"},
    "git":       {"GIT_TOOLS"},
}

_PHASE_PKG_MANAGER: dict[str, str] = {
    "system": "apt",
    "pip":    "pip3",
    "go":     "go",
    "brew":   "brew",
    "brew-cask": "brew",
    "git":    "git",
}


def _load_dep_manifest() -> dict:
    try:
        if _DEP_MANIFEST_PATH.is_file():
            data = json.loads(_DEP_MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "tools" in data:
                return data
    except Exception:
        pass
    return {"schema_version": "1.0", "tools": {}}


def _save_dep_manifest(manifest: dict) -> None:
    try:
        _DEP_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DEP_MANIFEST_PATH.write_text(
            json.dumps(manifest, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        warn(f"Could not save dependency manifest: {exc}")


def _record_dep(tool_name: str, dep_name: str, method: str,
                pkg_manager: str) -> None:
    m = _load_dep_manifest()
    tool_entry = m["tools"].setdefault(tool_name, {"deps": []})
    for d in tool_entry["deps"]:
        if d["name"] == dep_name and d["method"] == method:
            return
    tool_entry["deps"].append({
        "name":        dep_name,
        "method":      method,
        "pkg_manager": pkg_manager,
        "timestamp":   datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _save_dep_manifest(m)


def _get_tool_deps(tool_name: str) -> list[dict]:
    m = _load_dep_manifest()
    return m["tools"].get(tool_name, {}).get("deps", [])


def _get_all_tool_deps() -> dict:
    return _load_dep_manifest()["tools"]


def _remove_tool_deps(tool_name: str) -> None:
    m = _load_dep_manifest()
    if tool_name in m["tools"]:
        del m["tools"][tool_name]
        _save_dep_manifest(m)


def _find_shared_deps(tool_name: str) -> list[tuple[str, list[str]]]:
    m = _load_dep_manifest()
    my_deps = m["tools"].get(tool_name, {}).get("deps", [])
    shared = []
    for dep in my_deps:
        others = []
        for other_name, other_entry in m["tools"].items():
            if other_name == tool_name:
                continue
            for od in other_entry.get("deps", []):
                if od["name"] == dep["name"] and od["method"] == dep["method"]:
                    others.append(other_name)
                    break
        if others:
            shared.append((dep["name"], others))
    return shared


def _pkg_manager_for_phase(phase: str, os_type: str = "") -> str:
    if phase == "system":
        if os_type in ("kali", "ubuntu", "linux"):
            return "apt"
        if os_type == "rhel":
            return "dnf"
        if os_type == "suse":
            return "zypper"
        if os_type == "macos":
            return "brew"
        return "apt"
    return _PHASE_PKG_MANAGER.get(phase, phase)


def _dpkg_lock_recover(env: dict) -> None:
    """Kill orphaned apt/dpkg processes and repair dpkg state after a timeout."""
    for proc_name in ("dpkg", "apt-get", "apt"):
        subprocess.run(["killall", "-9", proc_name],
                       capture_output=True, check=False)
    time.sleep(2)
    for lock in ("/var/lib/dpkg/lock", "/var/lib/dpkg/lock-frontend",
                 "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock"):
        try:
            os.remove(lock)
        except FileNotFoundError:
            pass
    subprocess.run(["dpkg", "--configure", "-a"],
                   env=env, capture_output=True, timeout=120, check=False)


def _apt_pkg_installed(pkg: str) -> bool:
    r = subprocess.run(
        ["dpkg-query", "-W", "--showformat=${Status}", pkg],
        capture_output=True, text=True,
    )
    return "install ok installed" in r.stdout


def _log(phase: str, name: str, description: str,
         success: bool, stdout: str = "", stderr: str = "", error: str = "") -> None:
    entry = {
        "phase":       phase,
        "name":        name,
        "description": description,
        "success":     success,
        "stdout":      stdout,
        "stderr":      stderr,
        "error":       error,
    }
    _INSTALL_LOG.append(entry)
    if not success:
        _FAILURES.append(entry)
    sym = f"{_G}[+]{_R}" if success else f"{_RD}[!]{_R}"
    print(f"  {sym}  {name}  —  {description[:60]}", flush=True)
    if not success and (stderr or error):
        snippet = (stderr or error).strip().splitlines()
        if len(snippet) > 4:
            lines = [snippet[0], "  …"] + snippet[-3:]
        else:
            lines = snippet
        for ln in lines:
            print(f"      {_RD}{ln}{_R}", flush=True)

    if success and _DEP_TOOL_MAP and phase in _PHASE_TO_PKG_KEYS:
        skip = stdout and "skipped" in stdout.lower()
        if not skip:
            os_type = detect_os()
            pm = _pkg_manager_for_phase(phase, os_type)
            for key in _PHASE_TO_PKG_KEYS[phase]:
                tools = _DEP_TOOL_MAP.get((key, name))
                if tools:
                    dep_id = name
                    if phase == "git":
                        for mk, mv in _DEP_TOOL_MAP.items():
                            if mk[0] == key and "/" in mk[1] and mv == tools:
                                dep_id = mk[1]
                                break
                    for tool in tools:
                        _record_dep(tool, dep_id, phase, pm)
                    break


def _print_failure_summary() -> None:
    """Print a consolidated failure summary, then clear the list."""
    if not _FAILURES:
        return
    print(f"\n{_BLD}{_RD}{'═' * 60}{_R}")
    print(f"{_BLD}{_RD}  FAILURES ({len(_FAILURES)}){_R}")
    print(f"{_BLD}{_RD}{'═' * 60}{_R}")
    for i, f in enumerate(_FAILURES, 1):
        phase = f.get("phase", "?")
        name  = f.get("name", "?")
        desc  = f.get("description", "")
        detail = (f.get("error") or f.get("stderr") or "").strip()
        print(f"\n  {_RD}{i}. [{phase}] {name}{_R}")
        if desc:
            print(f"     {desc[:120]}")
        if detail:
            for ln in detail.splitlines()[:4]:
                print(f"     {_Y}{ln[:120]}{_R}")
    print(f"\n{_BLD}{_RD}{'═' * 60}{_R}\n")
    _FAILURES.clear()


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
def _apt_lists_fresh(max_age_days=30):
    """Return True if apt package lists were updated within max_age_days."""
    import time
    apt_lists = Path("/var/lib/apt/lists/")
    if not apt_lists.is_dir():
        return False
    try:
        mtime = max(
            (f.stat().st_mtime for f in apt_lists.iterdir()
             if f.is_file() and not f.name.startswith(".")),
            default=0)
    except OSError:
        return False
    if mtime == 0:
        return False
    age_days = (time.time() - mtime) / 86400
    return age_days < max_age_days


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
        if _apt_lists_fresh(30):
            info("apt lists are fresh (< 30 days) — skipping apt update/upgrade.")
        else:
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
    items = merged.get(pkg_list_key, [])
    if not items:
        info("No system packages requested.")
    elif "apt" in pm:
        all_pkgs = [it[0] if isinstance(it, (list, tuple)) else it for it in items]
        desc_map = {(it[0] if isinstance(it, (list, tuple)) else it):
                    (it[1] if isinstance(it, (list, tuple)) and len(it) >= 2 else "")
                    for it in items}
        cmd = [pm, "install", "-y", "-q",
               "-o", "Dpkg::Options::=--force-confold"] + all_pkgs
        try:
            r = _spin_run("apt batch install", cmd, capture_output=True,
                          text=True, env=env, timeout=900)
            if r.returncode != 0:
                warn("apt batch returned non-zero — will retry failures individually.")
        except subprocess.TimeoutExpired:
            warn("apt batch install timed out — recovering dpkg state …")
            _dpkg_lock_recover(env)
        except Exception as exc:
            warn(f"apt batch install failed: {exc}")
            _dpkg_lock_recover(env)
        failed = [p for p in all_pkgs if not _apt_pkg_installed(p)]
        for pkg_name in all_pkgs:
            if pkg_name not in failed:
                _log("system", pkg_name, desc_map.get(pkg_name, ""), True,
                     stdout="installed")
        if failed:
            info(f"Retrying {len(failed)} package(s) individually …")
            for pkg_name in failed:
                try:
                    r2 = _spin_run(pkg_name,
                        [pm, "install", "-y", "-q", pkg_name,
                         "-o", "Dpkg::Options::=--force-confold"],
                        capture_output=True, text=True, env=env, timeout=300)
                    ok = r2.returncode == 0 or _apt_pkg_installed(pkg_name)
                    _log("system", pkg_name, desc_map.get(pkg_name, ""), ok,
                         stdout=r2.stdout, stderr=r2.stderr)
                except subprocess.TimeoutExpired:
                    _dpkg_lock_recover(env)
                    ok = _apt_pkg_installed(pkg_name)
                    _log("system", pkg_name, desc_map.get(pkg_name, ""), ok,
                         error="timed out" if not ok else "installed (completed before kill)")
                except Exception as exc:
                    _log("system", pkg_name, desc_map.get(pkg_name, ""), False,
                         error=str(exc))
    else:
        for item in items:
            pkg, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
            try:
                if "dnf" in pm or "yum" in pm:
                    cmd = [pm, "install", "-y", "-q", pkg]
                else:
                    cmd = [pm, "install", "-y", pkg]
                r = _spin_run(pkg, cmd, capture_output=True, text=True,
                             env=env, timeout=300)
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


def _pip_installed() -> dict:
    """Return {normalised_name: version} for every installed pip package."""
    try:
        from importlib.metadata import distributions
        return {d.metadata["Name"].lower().replace("-", "_"): d.version
                for d in distributions() if d.metadata.get("Name")}
    except Exception:
        return {}


def _pip_spec_satisfied(spec: str, installed: dict) -> bool:
    """Check whether a pip spec like 'requests>=2.28.0' is already satisfied."""
    name = spec.split(">=")[0].split("==")[0].split("<=")[0].split("!=")[0] \
               .split("<")[0].split(">")[0].split("[")[0].strip().lower().replace("-", "_")
    if name not in installed:
        return False
    ver_part = spec[len(name):].strip().lstrip("[").split("]")[0] if "[" in spec else spec[len(name):].strip()
    if not ver_part or ver_part.startswith("["):
        return True
    if ver_part.startswith(">="):
        try:
            from packaging.version import Version
            return Version(installed[name]) >= Version(ver_part[2:])
        except Exception:
            return True
    if ver_part.startswith("=="):
        return installed[name] == ver_part[2:]
    return True


def _fix_apt_pip_conflicts() -> None:
    """Fix apt-installed Python packages whose RECORD file is missing.

    Debian/Ubuntu/Kali install packages like certifi via apt (python3-certifi)
    which creates .dist-info directories without a RECORD file. pip's dependency
    resolver later refuses to install packages that depend on them because it
    can't verify the installation. Force-reinstalling via pip creates the
    missing RECORD and unblocks downstream installs.
    """
    _APT_CONFLICT_PKGS = ["certifi", "urllib3", "requests", "charset-normalizer", "idna"]
    try:
        from importlib.metadata import distributions
        dist_map = {}
        for d in distributions():
            name = d.metadata.get("Name")
            if name:
                dist_map[name.lower().replace("-", "_")] = d
    except Exception:
        return
    fixed = []
    for pkg in _APT_CONFLICT_PKGS:
        d = dist_map.get(pkg)
        if d is None:
            continue
        record = d._path / "RECORD"
        if record.exists():
            continue
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--break-system-packages", "--ignore-installed", pkg],
                capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                fixed.append(pkg)
        except Exception:
            pass
    if fixed:
        info(f"pip: fixed missing RECORD for apt-managed package(s): {', '.join(fixed)}")


def _phase_pip(merged: dict) -> None:
    print(f"\n{_BLD}── Phase: pip packages ───────────────────────────────{_R}")
    _fix_apt_pip_conflicts()
    _offline_urls = merged.get("OFFLINE_BUNDLE_URLS", {})
    installed = _pip_installed()
    skipped = 0
    for item in merged.get("PIP_PACKAGES", []):
        spec, desc = (item[0], item[1]) if len(item) >= 2 else (item[0], "")
        if _pip_spec_satisfied(spec, installed):
            _log("pip", spec, desc, True, stdout="already installed — skipped")
            skipped += 1
            continue
        try:
            r = _spin_run(spec,
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--break-system-packages", spec],
                capture_output=True, text=True, timeout=1200)
            _log("pip", spec, desc, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
            if r.returncode != 0 and _offline_urls:
                _pkg_name = spec.split(">=")[0].split("==")[0].split("[")[0].strip()
                if _pkg_name in _offline_urls:
                    warn(f"pip failed for {_pkg_name} — trying offline bundle …")
                    _install_offline_bundle_arsenal(_pkg_name, _offline_urls[_pkg_name])
        except Exception as exc:
            _log("pip", spec, desc, False, error=str(exc))
    if skipped:
        info(f"pip: {skipped} package(s) already satisfied — skipped")


_MIN_GO = (1, 22)


def _go_version(go_bin: str) -> tuple:
    """Parse Go version from 'go version' output → (major, minor, patch)."""
    try:
        r = subprocess.run([go_bin, "version"], capture_output=True, text=True, timeout=10)
        m = re.search(r'go(\d+)\.(\d+)(?:\.(\d+))?', r.stdout)
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except Exception:
        pass
    return (0, 0, 0)


def _ensure_modern_go(go_bin: str) -> str:
    """If installed Go is below _MIN_GO, download the latest from go.dev.

    Returns the path to a usable go binary (upgraded or original).
    """
    ver = _go_version(go_bin)
    if ver[:2] >= _MIN_GO:
        ok(f"Go {ver[0]}.{ver[1]}.{ver[2]} is recent enough.")
        return go_bin

    warn(f"Go {ver[0]}.{ver[1]}.{ver[2]} is too old "
         f"(need {_MIN_GO[0]}.{_MIN_GO[1]}+) — upgrading from go.dev …")

    arch = platform.machine()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armv6l",
                "arm64": "arm64", "i686": "386", "i386": "386"}
    go_arch = arch_map.get(arch, "amd64")
    go_os = "darwin" if sys.platform == "darwin" else "linux"

    latest = None
    try:
        req = urllib.request.Request(
            "https://go.dev/dl/?mode=json",
            headers={"User-Agent": f"Arsenal/{VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            releases = json.loads(r.read())
        for rel in releases:
            if rel.get("stable"):
                latest = rel["version"]
                break
    except Exception as exc:
        warn(f"Could not query go.dev for latest version: {exc}")

    if not latest:
        warn("Cannot determine latest Go version — keeping old Go.")
        return go_bin

    tarball = f"{latest}.{go_os}-{go_arch}.tar.gz"
    url = f"https://go.dev/dl/{tarball}"
    dest = f"/tmp/{tarball}"

    info(f"Downloading {url} …")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        warn(f"Go download failed: {exc}")
        return go_bin

    info(f"Installing {latest} to /usr/local/go …")
    try:
        old_go = Path("/usr/local/go")
        if old_go.is_dir():
            shutil.rmtree(old_go)
        subprocess.run(["tar", "-C", "/usr/local", "-xzf", dest],
                       check=True, timeout=120)
        Path(dest).unlink(missing_ok=True)
    except Exception as exc:
        warn(f"Go tarball extraction failed: {exc}")
        return go_bin

    new_go = "/usr/local/go/bin/go"
    if not Path(new_go).is_file():
        warn("Go binary not found after install — keeping old Go.")
        return go_bin

    for binary in ("go", "gofmt"):
        link = Path(f"/usr/local/bin/{binary}")
        target = Path(f"/usr/local/go/bin/{binary}")
        if target.is_file():
            link.unlink(missing_ok=True)
            link.symlink_to(target)

    nv = _go_version(new_go)
    ok(f"Go upgraded to {nv[0]}.{nv[1]}.{nv[2]}")
    return new_go


def _phase_go(merged: dict) -> None:
    go = shutil.which("go")
    if not go:
        warn("go not found — skipping Go phase.")
        warn("  Install Go: https://go.dev/dl/")
        return
    go = _ensure_modern_go(go)
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
                capture_output=True, text=True, env=env, timeout=1800)
            _log("go", binary, pkg, r.returncode == 0,
                 stdout=r.stdout, stderr=r.stderr)
            if r.returncode != 0 and binary in _offline_urls:
                warn(f"go install failed for {binary} — trying offline bundle …")
                _install_offline_bundle_arsenal(binary, _offline_urls[binary])
        except subprocess.TimeoutExpired:
            _log("go", binary, pkg, False, error="go install timed out after 1800s")
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
def install_apps_phased(app_names: list, install_modes: "dict | None" = None) -> None:
    """
    Install one or more apps using a combined phased approach:
      apt/dnf/zypper (once) → brew → pip → go → git
    Packages are merged and deduplicated across all apps before execution.
    """
    _INSTALL_LOG.clear()
    _FAILURES.clear()
    os_type = detect_os()
    info(f"Detected OS: {os_type}")

    _git_e = _git_env()

    # Step 1: clone / pull all repos first
    for name in app_names:
        app         = APP_INDEX[name]
        install_dir = _install_dir(app)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        if install_dir.is_dir():
            info(f"{name} already present — pulling latest…")
            _git_pull_with_pycache_retry(install_dir, _git_e)
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
                continue
            _ensure_filemode_off(install_dir)

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
    # Tools that don't parse --skip-* flags will redo apt/pip from scratch,
    # which on a Pi easily exceeds the timeout. Detect this via a quick AST
    # check for "skip" in the source; if absent, skip the post-install entirely
    # since Arsenal already handled all their deps in the merged phases above.
    _modes = install_modes or {}
    for name in app_names:
        install_dir = _install_dir(APP_INDEX[name])
        installer   = install_dir / "install.py"
        if installer.exists():
            try:
                _src = installer.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                _src = ""
            _has_skip = "skip_apt" in _src or "skip-apt" in _src or "skip.apt" in _src
            if not _has_skip:
                ok(f"{name} post-install skipped (deps handled by Arsenal).")
                continue
            info(f"Running post-install steps from {name}/install.py…")
            extra = ["--skip-apt", "--skip-pip", "--skip-go"]
            if name in _modes:
                extra += [f"--mode={_modes[name]}"]
            try:
                r = subprocess.run(
                    [sys.executable, str(installer)] + extra,
                    cwd=str(install_dir),
                    capture_output=True, text=True, timeout=1200,
                )
                if r.returncode == 0:
                    ok(f"{name} post-install complete.")
                else:
                    warn(f"{name} post-install returned {r.returncode}.")
                    combined = []
                    if r.stdout:
                        combined += r.stdout.strip().splitlines()
                    if r.stderr:
                        combined += r.stderr.strip().splitlines()
                    for ln in combined[-30:]:
                        print(f"    {_Y}{ln}{_R}", flush=True)
                    _FAILURES.append({
                        "phase": "post-install",
                        "name":  name,
                        "description": f"install.py exited with code {r.returncode}",
                        "success": False,
                        "stderr": "\n".join(combined[-10:]),
                        "error": f"install.py returned {r.returncode}",
                    })
            except subprocess.TimeoutExpired:
                warn(f"{name} post-install timed out.")
                _FAILURES.append({
                    "phase": "post-install",
                    "name":  name,
                    "description": "install.py timed out after 1200s",
                    "success": False,
                    "stderr": "",
                    "error": "timed out after 1200s",
                })
            except Exception as exc:
                warn(f"{name} post-install error: {exc}")
                _FAILURES.append({
                    "phase": "post-install",
                    "name":  name,
                    "description": str(exc)[:120],
                    "success": False,
                    "stderr": "",
                    "error": str(exc),
                })

    # Step 5: write service files (plist / systemd unit) for Flask apps
    for name in app_names:
        app = APP_INDEX[name]
        _write_plist(app)
        _write_systemd_unit(app)

    # Step 6: start services for newly installed apps
    if os_type == "macos":
        for name in app_names:
            if _modes.get(name) == "server":
                continue
            svc = APP_INDEX[name].get("service")
            if not svc:
                continue
            label = f"com.pr0t31n.{name.lower()}"
            plist_path = Path(f"/Library/LaunchDaemons/{label}.plist")
            if not plist_path.exists():
                continue
            info(f"Loading {label}…")
            rl = subprocess.run(["launchctl", "load", "-w", str(plist_path)],
                                capture_output=True, text=True, timeout=10)
            if rl.returncode == 0:
                ok(f"{label} loaded.")
            else:
                warn(f"launchctl load {plist_path} returned {rl.returncode}.")

    if shutil.which("systemctl") and os_type not in ("macos", "windows"):
        for name in app_names:
            if _modes.get(name) == "server":
                continue
            svc = APP_INDEX[name].get("service")
            if not svc:
                continue
            # Verify the unit file exists before attempting to start
            chk = subprocess.run(
                ["systemctl", "cat", svc],
                capture_output=True, text=True)
            if chk.returncode != 0:
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

    # Step 7: handle optional dependencies for newly installed apps
    for name in app_names:
        _handle_optional_deps(APP_INDEX[name], is_update=False)

    _print_failure_summary()
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
        st["needs_update"] = bool(lv and gv and lv not in ("unknown", gv)
                                    and _version_gt(gv, lv))

    outdated = [a for a in APPS if status[a["name"]]["needs_update"]]
    if not outdated:
        ok("All installed apps are up to date.")
    else:
        print(f"\n{_Y}Updates available:{_R}")
        for app in outdated:
            st = status[app["name"]]
            print(f"  {_Y}▲{_R}  {app['name']:<15}  "
                  f"{st['local_ver']}  →  {_G}{st['github_ver']}{_R}")


def _prompt_install_modes(app_names: list) -> dict:
    """Prompt for install mode on any app that has install_modes defined."""
    modes: dict = {}
    for name in app_names:
        app_modes = APP_INDEX[name].get("install_modes")
        if not app_modes:
            continue
        print(f"\n{_BLD}  {name} — Select install mode:{_R}")
        for i, m in enumerate(app_modes, 1):
            print(f"    {i}) {m['label']}")
            print(f"       {_DIM}{m['desc']}{_R}")
        try:
            choice = input(f"\n  Select [1-{len(app_modes)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(app_modes):
                modes[name] = app_modes[idx]["key"]
            else:
                modes[name] = app_modes[0]["key"]
        except (EOFError, KeyboardInterrupt, ValueError):
            modes[name] = app_modes[0]["key"]
        info(f"{name} install mode: {modes[name]}")
    return modes


def _do_upgrade(status: dict, checked: "list[str] | None" = None) -> None:
    """Upgrade selected apps marked needs_update; mutate status in place.

    When *checked* is given (list of tool names), only those tools are
    upgraded.  When *checked* is ``None`` or empty, ALL installed apps
    with ``needs_update`` are upgraded (legacy / fallback behaviour).

    Runs ``apt update`` + ``apt upgrade -y`` at most ONCE across all apps,
    then passes ``--skip-apt`` to each tool's ``install.py`` so they skip
    their own redundant apt refresh.
    """
    if checked:
        _names = set(checked)
        outdated = [a for a in APPS
                    if a["name"] in _names and status[a["name"]]["needs_update"]]
    else:
        outdated = [a for a in APPS if status[a["name"]]["needs_update"]]
    if not outdated:
        ok("All installed apps are up to date.")
        return
    _FAILURES.clear()
    print(f"\n{_BLD}Upgrading:{_R}")
    for app in outdated:
        print(f"  {_Y}▲{_R}  {app['name']}")
    print()

    # ── Prompt for install modes where applicable ────────────────────────
    _modes = _prompt_install_modes([a["name"] for a in outdated])

    # ── Run apt update + upgrade ONCE before any per-app install.py ────────
    os_type = detect_os()
    _ran_apt = False
    if os_type not in ("macos", "windows"):
        pm = shutil.which("apt-get") or shutil.which("apt")
        if pm and "apt" in pm:
            if _apt_lists_fresh(30):
                info("apt lists are fresh (< 30 days) — skipping apt update/upgrade.")
                _ran_apt = True
            else:
                env = os.environ.copy()
                env["DEBIAN_FRONTEND"] = "noninteractive"
                print(f"{_BLD}── Phase: apt update + upgrade (once) ──────────────{_R}")
                try:
                    subprocess.run([pm, "update", "-qq"], env=env, timeout=180)
                except subprocess.TimeoutExpired:
                    warn("apt update timed out — continuing with cached package lists.")
                except Exception as exc:
                    warn(f"apt update failed: {exc}")
                try:
                    subprocess.run([pm, "upgrade", "-y", "-qq"], env=env, timeout=3600)
                except subprocess.TimeoutExpired:
                    warn("apt upgrade timed out after 60 min — continuing.")
                except Exception as exc:
                    warn(f"apt upgrade failed: {exc}")
                _ran_apt = True
                print()

    skip_flags = ["--skip-apt"] if _ran_apt else []
    for app in outdated:
        name = app["name"]
        extra = list(skip_flags)
        if name in _modes:
            extra.append(f"--mode={_modes[name]}")
        _update_app(app, installer_args=extra)
        status[name]["local_ver"]    = get_local_version(app)
        status[name]["needs_update"] = False
    _print_failure_summary()
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
        # Exit code 1 with "nothing to commit" is not an error — but still
        # attempt the push in case prior commits remain unpushed.
        if "nothing to commit" in (r2.stdout + r2.stderr).lower():
            pass  # fall through to push
        else:
            warn(f"{name}: git commit returned {r2.returncode} — {r2.stderr.strip()[:80]}")

    # Detect current branch (main or master)
    _head_ref = _git_run(
        ["git", "-C", str(install_dir), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, env=git_e,
    )
    branch = _head_ref.stdout.strip() if _head_ref.returncode == 0 else "main"

    r3 = _git_run(
        ["git", "-C", str(install_dir), "push", "origin", branch],
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
def execute_changes(selection: dict, status: dict[str, dict]) -> bool:
    to_install   = selection.get("to_install",   [])
    to_uninstall = selection.get("to_uninstall", [])
    to_restart   = selection.get("to_restart",   [])

    if not to_install and not to_uninstall and not to_restart:
        info("No changes selected.")
        return False

    # Pure restart — skip install/uninstall confirmation
    if to_restart and not to_install and not to_uninstall:
        for name in to_restart:
            print(f"{_BLD}{'─' * 50}{_R}")
            print(f"{_BLD}  Restarting {name}{_R}")
            print(f"{_BLD}{'─' * 50}{_R}")
            restart_app(APP_INDEX[name])
            print()
        ok("All operations complete.")
        return True

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
        return False

    if ans != "y":
        info("Aborted.")
        return False

    print()

    # ── Prompt for install modes where applicable ──────────────────────
    install_modes = _prompt_install_modes(to_install)

    # ── Installs — phased across all selected apps ──────────────────────
    if to_install:
        _INSTALL_LOG.clear()
        print(f"\n{_BLD}{'─' * 50}{_R}")
        print(f"{_BLD}  Installing (phased): {', '.join(to_install)}{_R}")
        print(f"{_BLD}{'─' * 50}{_R}")
        install_apps_phased(to_install, install_modes=install_modes)
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
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    check_root()

    # Banner
    print(f"{_C}{BANNER}{_R}")
    print(f"{_BLD}  Tool installer and manager  ·  Arsenal v{VERSION}{_R}")
    print(f"  Platform: {SYSTEM}   Install base: {INSTALL_BASE}\n")

    # Check system prerequisites (git, pip3, curl, go, etc.)
    check_prerequisites()

    # Optional --token flag for authenticated GitHub access (public repos don't need it)
    _parse_token_flag()

    # Verify GitHub reachability
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
            _clean_input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")

        elif action == "upgrade":
            _do_upgrade(status, checked=selection.get("checked"))
            _clean_input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")

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
            _clean_input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")

        else:  # execute — install / uninstall / restart
            _did_work = execute_changes(selection, status)
            if _did_work:
                for name in selection.get("to_install", []):
                    app = APP_INDEX[name]
                    status[name]["installed"]      = _install_dir(app).is_dir()
                    status[name]["local_ver"]      = get_local_version(app)
                    status[name]["service_status"] = get_service_status(app)
                    status[name]["needs_update"]   = False
                for name in selection.get("to_uninstall", []):
                    app = APP_INDEX[name]
                    still_exists = _install_dir(app).is_dir()
                    status[name]["installed"]      = still_exists
                    status[name]["local_ver"]      = get_local_version(app) if still_exists else None
                    status[name]["service_status"] = get_service_status(app) if still_exists else ""
                    status[name]["needs_update"]   = False
                _flask_restarted = [n for n in selection.get("to_restart", []) if APP_INDEX[n].get("flask")]
                if _flask_restarted:
                    time.sleep(4)
                for name in selection.get("to_restart", []):
                    app = APP_INDEX[name]
                    if app.get("flask"):
                        status[name]["service_status"] = get_service_status(app)
                        status[name]["running_ver"]    = get_running_version(app)
            _clean_input(f"\n{_BLD}  Press Enter to return to menu…{_R} ")


# ── Structured CLI (Nexus arsenal_bridge integration) ─────────────────
# Nexus uses this mode for unattended installs over agent/SSH dispatch.
# Mode is entered by passing --json --non-interactive plus a command.
# Output: a single JSON object on stdout, exit code 0=success non-zero=error.
def _cli_main() -> int:
    import argparse, json as _json, sys as _sys, traceback as _tb
    p = argparse.ArgumentParser(prog="arsenal", add_help=True)
    p.add_argument("--json", action="store_true", help="emit JSON on stdout")
    p.add_argument("--non-interactive", action="store_true",
                   help="never prompt; required for unattended mode")
    p.add_argument("--token", default=None,
                   help="GitHub PAT for authenticated API access (optional)")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="dump current install status table")
    p_install = sub.add_parser("install", help="install a tool")
    p_install.add_argument("--tool", required=True)
    p_uninst  = sub.add_parser("uninstall", help="uninstall a tool")
    p_uninst.add_argument("--tool", required=True)
    p_upgrade = sub.add_parser("upgrade", help="upgrade a tool to latest")
    p_upgrade.add_argument("--tool", required=True)
    p_check   = sub.add_parser("check-updates",
                               help="report whether each tool needs an update")
    args = p.parse_args()
    if args.token:
        _GH_CREDS["user"] = "token"
        _GH_CREDS["token"] = args.token
    if not (args.json and args.non_interactive and args.command):
        return -1   # signal: fall through to interactive
    out: dict = {"command": args.command, "arsenal_version": VERSION}
    # Non-interactive prereq check — report missing critical tools in JSON
    _prereq_missing = [b for b, _, _, _, _, _ in _PREREQS_CRITICAL if not shutil.which(b)]
    if _prereq_missing and args.command in ("install", "upgrade"):
        out.update({"ok": False,
                    "error": f"missing critical prerequisites: {', '.join(_prereq_missing)}",
                    "missing_prereqs": _prereq_missing})
        print(_json.dumps(out)); return 4
    try:
        if args.command == "status":
            status = load_status()
            refresh_local_status(status, with_github=False)
            out["tools"] = {
                n: {"installed":  s.get("installed", False),
                    "local_ver":  s.get("local_ver"),
                    "github_ver": s.get("github_ver"),
                    "needs_update": bool(s.get("needs_update")),
                    "service_status": s.get("service_status", "")}
                for n, s in status.items()}
            out["ok"] = True
        elif args.command == "check-updates":
            status = load_status()
            refresh_local_status(status, with_github=True)
            out["needs_update"] = [n for n, s in status.items()
                                   if s.get("needs_update")]
            out["ok"] = True
        elif args.command in ("install", "uninstall", "upgrade"):
            tool = args.tool
            if tool not in APP_INDEX:
                out.update({"ok": False, "error": f"unknown tool: {tool}"})
                print(_json.dumps(out)); return 2
            app = APP_INDEX[tool]
            pre_ver = get_local_version(app)
            if args.command == "install":
                install_app(app)
            elif args.command == "uninstall":
                uninstall_app(app)
            else:  # upgrade — git pull (handled by install_app for existing dirs)
                install_app(app)
            post_ver = get_local_version(app)
            installed_now = _install_dir(app).is_dir()
            out.update({"ok": True, "tool": tool,
                        "pre_version": pre_ver,
                        "post_version": post_ver,
                        "installed_after": installed_now})
        else:
            out.update({"ok": False, "error": "unknown command"})
            print(_json.dumps(out)); return 2
    except SystemExit:
        raise
    except Exception as ex:
        out.update({"ok": False,
                    "error": f"{type(ex).__name__}: {ex}",
                    "traceback": _tb.format_exc()})
        print(_json.dumps(out)); return 3
    print(_json.dumps(out))
    return 0


if __name__ == "__main__":
    import sys as _sys
    if "--json" in _sys.argv and "--non-interactive" in _sys.argv:
        rc = _cli_main()
        if rc != -1:
            _sys.exit(rc)
    main()
