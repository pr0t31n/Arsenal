#!/usr/bin/env python3
"""
install.py — Arsenal installer
================================
Verifies the runtime environment, makes arsenal.py executable, and installs
a system-wide `arsenal` command accessible to all users including root/sudo.

Platform handling:
  Linux  — symlink in /usr/local/bin/arsenal  (takes priority in sudo PATH)
           fallback: /etc/profile.d/pr0t31n-arsenal.sh  (PATH export for all shells)
  macOS  — symlink in /usr/local/bin/arsenal
           fallback: /etc/paths.d/pr0t31n-arsenal  (path_helper for all users)

Usage:
    sudo python3 install.py
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

VERSION = "1.0.0"
SYSTEM  = platform.system()   # 'Linux' | 'Darwin'

# ─────────────────────────────────────────────────────────────────────────────
# Colour helpers
# ─────────────────────────────────────────────────────────────────────────────
_R   = "\033[0m"
_G   = "\033[32m"
_Y   = "\033[33m"
_RD  = "\033[31m"
_C   = "\033[36m"
_BLD = "\033[1m"

def ok(m:   str) -> None: print(f"{_G}[+]{_R} {m}", flush=True)
def info(m: str) -> None: print(f"{_C}[*]{_R} {m}", flush=True)
def warn(m: str) -> None: print(f"{_Y}[~]{_R} {m}", flush=True)
def err(m:  str) -> None: print(f"{_RD}[!]{_R} {m}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _banner() -> None:
    print(f"{_BLD}{'=' * 56}{_R}")
    print(f"{_BLD}  Arsenal {VERSION} — Installer{_R}")
    print(f"  Platform: {SYSTEM}")
    print(f"{_BLD}{'=' * 56}{_R}")
    print()


def _check_root() -> None:
    if os.geteuid() != 0:
        err("This installer requires root privileges.")
        print(f"  Run:  {_RD}sudo python3 install.py{_R}")
        sys.exit(1)
    ok("Running as root.")


def _check_python() -> None:
    info("Checking Python version…")
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 9):
        err(f"Python 3.9+ required; found {major}.{minor}.")
        sys.exit(1)
    ok(f"Python {major}.{minor} — OK")


def _check_git() -> None:
    info("Checking for git…")
    git = shutil.which("git")
    if not git:
        err("git not found. Arsenal requires git to clone and update repositories.")
        if SYSTEM == "Linux":
            print("  Install:  sudo apt-get install -y git")
        elif SYSTEM == "Darwin":
            print("  Install:  brew install git  (or install Xcode Command Line Tools)")
        sys.exit(1)
    ok(f"git found: {git}")


def _check_curses() -> None:
    info("Checking curses module…")
    try:
        import curses  # noqa: F401
        ok("curses — OK (stdlib)")
    except ImportError:
        err("curses module not found.")
        if SYSTEM == "Linux":
            print("  Install:  sudo apt-get install -y python3-curses")
        else:
            print("  curses is included with the standard macOS Python.")
        sys.exit(1)


def _make_executable(script: Path) -> None:
    info(f"Making {script.name} executable…")
    current = script.stat().st_mode
    script.chmod(
        current
        | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    )
    ok(f"Permissions set: {oct(script.stat().st_mode)[-4:]}")


def _ensure_shebang(script: Path) -> None:
    """Ensure arsenal.py starts with #!/usr/bin/env python3."""
    text = script.read_text(encoding="utf-8")
    if not text.startswith("#!"):
        info("Adding shebang to arsenal.py…")
        script.write_text("#!/usr/bin/env python3\n" + text, encoding="utf-8")
        ok("Shebang added.")
    else:
        ok("Shebang already present.")


# ─────────────────────────────────────────────────────────────────────────────
# Symlink installation  (/usr/local/bin/arsenal)
# ─────────────────────────────────────────────────────────────────────────────
def _install_symlink(script: Path) -> bool:
    """
    Create /usr/local/bin/arsenal → script.
    Returns True on success, False if the directory is not writable.
    """
    bin_dir = Path("/usr/local/bin")
    link    = bin_dir / "arsenal"

    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return False

    # Remove stale link / regular file
    if link.exists() or link.is_symlink():
        link.unlink()
        info("Removed existing /usr/local/bin/arsenal.")

    link.symlink_to(script)
    ok(f"Symlink: {link}  →  {script}")

    # Verify it resolves and is executable
    if os.access(str(link), os.X_OK):
        ok("Verification: `arsenal` is executable via symlink.")
    else:
        warn("Symlink created but may not be executable — check file permissions.")

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: PATH via profile.d (Linux) or paths.d (macOS)
# ─────────────────────────────────────────────────────────────────────────────
def _install_path_linux(script_dir: Path) -> None:
    """
    Write /etc/profile.d/pr0t31n-arsenal.sh so the directory is on PATH
    for all users including root (sourced by bash and most POSIX shells).
    """
    profile_d = Path("/etc/profile.d")
    sh_file   = profile_d / "pr0t31n-arsenal.sh"

    try:
        profile_d.mkdir(parents=True, exist_ok=True)
        sh_file.write_text(
            f'# pr0t31n Arsenal — added by install.py\n'
            f'export PATH="$PATH:{script_dir}"\n',
            encoding="utf-8",
        )
        sh_file.chmod(0o644)
        ok(f"PATH export written: {sh_file}")
        warn("Open a new shell (or run `source /etc/profile`) for the PATH to take effect.")
    except PermissionError:
        err(f"Cannot write to {profile_d} — run as root.")


def _install_path_macos(script_dir: Path) -> None:
    """
    Write /etc/paths.d/pr0t31n-arsenal so macOS path_helper includes the
    directory for all users including root.
    """
    paths_d   = Path("/etc/paths.d")
    path_file = paths_d / "pr0t31n-arsenal"

    try:
        paths_d.mkdir(parents=True, exist_ok=True)
        path_file.write_text(str(script_dir) + "\n", encoding="utf-8")
        path_file.chmod(0o644)
        ok(f"paths.d entry written: {path_file}")
        warn("Open a new shell for the PATH change to take effect.")
    except PermissionError:
        err(f"Cannot write to {paths_d} — run as root.")


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────
def _verify() -> None:
    print()
    info("Post-install verification…")
    resolved = shutil.which("arsenal")
    if resolved:
        ok(f"`arsenal` resolves to: {resolved}")
    else:
        warn("`arsenal` not yet on PATH in this shell session.")
        print("  Open a new terminal, then run:  sudo arsenal")

    # Confirm sudo preserves PATH to /usr/local/bin
    if SYSTEM == "Linux":
        result = subprocess.run(
            ["sudo", "-n", "which", "arsenal"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            ok(f"`sudo arsenal` resolves to: {result.stdout.strip()}")
        else:
            warn("`sudo which arsenal` did not resolve — sudo may not include /usr/local/bin in PATH.")
            print("  Fix: add  Defaults secure_path=\"/usr/local/sbin:/usr/local/bin:...\"")
            print("       to /etc/sudoers (via visudo).")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _banner()
    _check_root()

    # ── Environment checks ───────────────────────────────────────────────────
    print()
    info("Checking environment…")
    _check_python()
    _check_git()
    _check_curses()

    # ── Locate arsenal.py ────────────────────────────────────────────────────
    print()
    info("Locating arsenal.py…")
    script = Path(__file__).parent.resolve() / "arsenal.py"
    if not script.exists():
        err(f"arsenal.py not found at expected location: {script}")
        sys.exit(1)
    ok(f"Found: {script}")

    # ── Prepare script ───────────────────────────────────────────────────────
    print()
    info("Preparing arsenal.py…")
    _ensure_shebang(script)
    _make_executable(script)

    # ── Install system-wide command ──────────────────────────────────────────
    print()
    info("Installing system-wide `arsenal` command…")

    symlink_ok = _install_symlink(script)

    if not symlink_ok:
        warn("/usr/local/bin not writable — falling back to PATH export.")
        if SYSTEM == "Linux":
            _install_path_linux(script.parent)
        elif SYSTEM == "Darwin":
            _install_path_macos(script.parent)
        else:
            warn(f"Unsupported platform ({SYSTEM}) for automatic PATH configuration.")
            print(f"  Add this directory to your PATH manually: {script.parent}")

    # ── Verification ─────────────────────────────────────────────────────────
    _verify()

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print(f"{_BLD}{'=' * 56}{_R}")
    ok("Arsenal installation complete.")
    print()
    print(f"  {_BLD}Run:{_R}  sudo arsenal")
    print()
    print(f"  Arsenal installs managed tools to: /opt/pr0t31n/")
    print(f"  Flask apps start as boot services on the local network:")
    print(f"    Quiver    http://<host>:5002")
    print(f"    Gauntlet  http://<host>:5000")
    print(f"    Spectre   http://<host>:5300")
    print(f"    Vantage   http://<host>:5100")
    print(f"{_BLD}{'=' * 56}{_R}")
    print()


if __name__ == "__main__":
    main()
