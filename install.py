#!/usr/bin/env python3
"""
Arsenal — pr0t31n toolkit manager
Install script for /opt/Arsenal/

Verifies the runtime environment, installs system packages (git, python3-curses,
psmisc/fuser), optionally installs python-docx for Word reports, makes arsenal.py
executable, and registers the `arsenal` command system-wide.

Supported platforms:
  Linux  (Kali, Ubuntu, Debian)  — apt
  Linux  (RHEL, CentOS, Fedora)  — dnf / yum
  Linux  (SUSE, openSUSE)        — zypper
  macOS                          — brew (git only; curses built into macOS Python)
  Windows 7+                     — guidance only; installs windows-curses via pip

Usage:
    sudo python3 install.py            # full install
    sudo python3 install.py --check    # dependency check only
    sudo python3 install.py --skip-apt # skip system packages
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# ─── ANSI colours ────────────────────────────────────────────────────────────
R  = "\033[1;31m"
G  = "\033[1;32m"
Y  = "\033[1;33m"
B  = "\033[1;34m"
C  = "\033[1;36m"
W  = "\033[0m"
# ─────────────────────────────────────────────────────────────────────────────

APP_NAME = "Arsenal"
APP_DIR  = Path(__file__).resolve().parent
VERSION  = "1.1.1"
SYSTEM   = platform.system()

# ── system package lists ──────────────────────────────────────────────────────
# python3-curses: curses TUI  |  psmisc: provides fuser (port killing on Linux)
APT_PKGS    = ["git", "python3", "python3-pip", "psmisc"]
DNF_PKGS    = ["git", "python3", "python3-pip", "python3-curses", "psmisc"]
ZYPPER_PKGS = ["git", "python3", "python3-pip", "python3-curses", "psmisc"]
BREW_PKGS   = ["git"]   # curses/lsof are pre-installed on macOS; python3 via Xcode/brew

# ── pip packages ─────────────────────────────────────────────────────────────
# python-docx: generates Word .docx install reports.
PIP_PKGS_COMMON  = ["python-docx>=1.1.0"]
# windows-curses: curses is not bundled with CPython on Windows
PIP_PKGS_WINDOWS = ["windows-curses>=2.3.0", "python-docx>=1.1.0"]


# ─── helpers ─────────────────────────────────────────────────────────────────

def banner() -> None:
    print(f"""
{C}╔══════════════════════════════════════════════╗
║        Arsenal — pr0t31n Toolkit Manager     ║
║        Install Script v{VERSION}                ║
╚══════════════════════════════════════════════╝{W}
""")


def info(msg: str) -> None: print(f"{G}[+]{W} {msg}")
def warn(msg: str) -> None: print(f"{Y}[!]{W} {msg}")
def err(msg:  str) -> None: print(f"{R}[✗]{W} {msg}")
def step(msg: str) -> None: print(f"\n{B}━━ {msg} ━━{W}")


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print(f"    {C}>{W} {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}")
    return result


def run_ok(cmd: list, **kwargs) -> bool:
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **kwargs)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _apt_pkg_installed(pkg: str) -> bool:
    """Return True if pkg is fully installed according to dpkg."""
    r = subprocess.run(
        ["dpkg-query", "-W", "--showformat=${Status}", pkg],
        capture_output=True, text=True,
    )
    return "install ok installed" in r.stdout



# ─── root / admin check ──────────────────────────────────────────────────────

def require_root() -> None:
    if SYSTEM == "Windows":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                err("This installer must be run as Administrator on Windows.")
                print("  Right-click Command Prompt → 'Run as administrator'")
                sys.exit(1)
        except Exception:
            warn("Could not verify admin status on Windows — proceeding.")
        return
    if os.geteuid() != 0:
        err("This installer requires root privileges.")
        print(f"  Run:  {R}sudo python3 install.py{W}")
        sys.exit(1)


# ─── OS detection ─────────────────────────────────────────────────────────────

def detect_os() -> str:
    """Return one of: macos apt dnf zypper windows linux"""
    if SYSTEM == "Darwin":  return "macos"
    if SYSTEM == "Windows": return "windows"
    os_release: dict = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    os_release[k] = v.strip('"')
    except FileNotFoundError:
        pass
    dist_id = os_release.get("ID", "").lower()
    id_like  = os_release.get("ID_LIKE", "").lower()
    if dist_id in ("kali", "ubuntu", "debian", "parrot") or "debian" in id_like:
        return "apt"
    if (dist_id in ("rhel", "centos", "rocky", "almalinux", "fedora")
            or "rhel" in id_like or "fedora" in id_like):
        return "dnf"
    if (dist_id in ("sles", "opensuse", "opensuse-leap", "opensuse-tumbleweed")
            or "suse" in id_like):
        return "zypper"
    return "apt"


# ─── phase 1: system packages ────────────────────────────────────────────────

def _brew_install(pkgs: list) -> None:
    brew_path = shutil.which("brew")
    if not brew_path:
        warn("Homebrew not found; skipping brew installs.")
        warn("  Install Homebrew from https://brew.sh then re-run this installer.")
        return
    sudo_user = os.environ.get("SUDO_USER", "")
    for pkg in pkgs:
        try:
            if os.getuid() == 0:
                if not sudo_user:
                    warn(f"Running as root with no SUDO_USER — cannot brew install {pkg}; skipping.")
                    continue
                run(["sudo", "-u", sudo_user, brew_path, "install", pkg])
            else:
                run([brew_path, "install", pkg])
        except RuntimeError as exc:
            warn(f"brew install {pkg} failed: {exc}")


def install_system_packages(os_type: str, args) -> None:
    step("Phase 1 — System Packages")
    if args.skip_apt:
        warn("--skip-system set; skipping system package install.")
        return

    if os_type == "windows":
        warn("Windows detected. Ensure the following are installed manually:")
        warn("  Python 3.9+: https://www.python.org/downloads/")
        warn("  Git:         https://git-scm.com/downloads")
        return

    if os_type == "macos":
        _brew_install(BREW_PKGS)
        return

    if os_type == "apt":
        info("Running apt update…")
        run(["apt-get", "update", "-y"])
        # Repair any broken package state (e.g. failed third-party postinst hooks)
        # before attempting our own installs.
        info("Repairing broken packages…")
        subprocess.run(["apt-get", "install", "-f", "-y"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info("Installing APT packages…")
        try:
            run(["apt-get", "install", "-y"] + APT_PKGS)
        except RuntimeError:
            # apt-get can return non-zero even when our packages installed
            # successfully — a common cause is third-party postinst hook
            # failures (e.g. sqlmap, commix) that are unrelated to Arsenal.
            missing = [p for p in APT_PKGS if not _apt_pkg_installed(p)]
            if missing:
                raise RuntimeError(
                    f"apt-get install failed and these packages are missing: {missing}"
                )
            warn("apt-get exited non-zero (likely an unrelated postinst hook failure).")
            warn("All required packages are installed — continuing.")

    elif os_type == "dnf":
        info("Installing DNF packages…")
        run(["dnf", "install", "-y"] + DNF_PKGS)

    elif os_type == "zypper":
        info("Installing zypper packages…")
        run(["zypper", "install", "-y"] + ZYPPER_PKGS)

    else:
        warn(f"Unknown OS type '{os_type}' — attempting apt install.")
        try:
            run(["apt-get", "update", "-y"])
            run(["apt-get", "install", "-y"] + APT_PKGS)
        except RuntimeError:
            warn("apt-get failed — install git, python3, python3-pip, and psmisc manually.")


# ─── phase 2: pip packages ───────────────────────────────────────────────────

def _pip_cmd() -> list:
    if shutil.which("pip3"):
        return ["pip3"]
    if shutil.which("pip"):
        return ["pip"]
    return [sys.executable, "-m", "pip"]


def install_pip_packages(os_type: str, args) -> None:
    step("Phase 2 — Pip Packages")
    if args.skip_pip:
        warn("--skip-pip set; skipping.")
        return
    pip  = _pip_cmd()
    pkgs = PIP_PKGS_WINDOWS if os_type == "windows" else PIP_PKGS_COMMON
    for pkg in pkgs:
        info(f"pip install {pkg} …")
        extra = [] if os_type == "windows" else ["--break-system-packages"]
        try:
            run(pip + ["install"] + extra + [pkg])
        except RuntimeError as exc:
            warn(f"pip install failed for {pkg}: {exc}")


# ─── phase 3: arsenal command setup ─────────────────────────────────────────

def _make_executable(script: Path) -> None:
    current = script.stat().st_mode
    script.chmod(
        current
        | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    )
    info(f"Permissions set: {oct(script.stat().st_mode)[-4:]}")


def _ensure_shebang(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    if not text.startswith("#!"):
        info("Adding shebang to arsenal.py…")
        script.write_text("#!/usr/bin/env python3\n" + text, encoding="utf-8")
        info("Shebang added.")
    else:
        info("Shebang already present.")


def _install_symlink(script: Path) -> bool:
    """Create /usr/local/bin/arsenal → script. Returns True on success."""
    bin_dir = Path("/usr/local/bin")
    link    = bin_dir / "arsenal"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return False
    if link.exists() or link.is_symlink():
        link.unlink()
        info("Removed existing /usr/local/bin/arsenal.")
    link.symlink_to(script)
    info(f"Symlink: {link}  →  {script}")
    if os.access(str(link), os.X_OK):
        info("Verification: `arsenal` is executable via symlink.")
    else:
        warn("Symlink created but may not be executable — check file permissions.")
    return True


def _install_path_linux(script_dir: Path) -> None:
    profile_d = Path("/etc/profile.d")
    sh_file   = profile_d / "pr0t31n-arsenal.sh"
    try:
        profile_d.mkdir(parents=True, exist_ok=True)
        sh_file.write_text(
            "# pr0t31n Arsenal — added by install.py\n"
            f'export PATH="$PATH:{script_dir}"\n',
            encoding="utf-8",
        )
        sh_file.chmod(0o644)
        info(f"PATH export written: {sh_file}")
        warn("Open a new shell (or run `source /etc/profile`) for PATH to take effect.")
    except PermissionError:
        err(f"Cannot write to {profile_d} — run as root.")


def _install_path_macos(script_dir: Path) -> None:
    paths_d   = Path("/etc/paths.d")
    path_file = paths_d / "pr0t31n-arsenal"
    try:
        paths_d.mkdir(parents=True, exist_ok=True)
        path_file.write_text(str(script_dir) + "\n", encoding="utf-8")
        path_file.chmod(0o644)
        info(f"paths.d entry written: {path_file}")
        warn("Open a new shell for the PATH change to take effect.")
    except PermissionError:
        err(f"Cannot write to {paths_d} — run as root.")


def setup_arsenal_command(os_type: str, script: Path, args) -> None:
    step("Phase 3 — Arsenal Command Setup")
    if args.skip_setup:
        warn("--skip-setup set; skipping command setup.")
        return

    if os_type == "windows":
        warn("Windows: add this directory to PATH manually, or create a launcher:")
        warn(f"  dir:     {script.parent}")
        warn(f"  example: echo @python \"{script}\" %* > arsenal.bat")
        return

    _ensure_shebang(script)
    _make_executable(script)

    symlink_ok = _install_symlink(script)
    if not symlink_ok:
        warn("/usr/local/bin not writable — falling back to PATH export.")
        if os_type in ("apt", "dnf", "zypper", "linux"):
            _install_path_linux(script.parent)
        elif os_type == "macos":
            _install_path_macos(script.parent)
        else:
            warn(f"Add this directory to your PATH manually: {script.parent}")


# ─── check mode ──────────────────────────────────────────────────────────────

def run_check(os_type: str) -> bool:
    step("Check Mode — Dependency Status")

    checks = [
        ("python3",  shutil.which("python3") or sys.executable),
        ("git",      shutil.which("git")),
    ]
    if os_type not in ("macos", "windows"):
        checks.append(("fuser (psmisc)", shutil.which("fuser")))

    all_ok = True
    for name, path in checks:
        if path and Path(path).exists():
            info(f"{name:24s} {G}OK{W}  ({path})")
        else:
            err(f"{name:24s} {R}MISSING{W}")
            all_ok = False

    curses_ok = run_ok([sys.executable, "-c", "import curses"])
    if curses_ok:
        info(f"{'python:curses':24s} {G}OK{W}  (stdlib)")
    else:
        err(f"{'python:curses':24s} {R}MISSING{W}  (install python3-curses or windows-curses)")
        all_ok = False

    docx_ok = run_ok([sys.executable, "-c", "import docx"])
    if docx_ok:
        info(f"{'python:docx':24s} {G}OK{W}  (installed)")
    else:
        err(f"{'python:docx':24s} {R}MISSING{W}")
        all_ok = False

    arsenal_path = shutil.which("arsenal")
    if arsenal_path:
        info(f"{'arsenal command':24s} {G}OK{W}  ({arsenal_path})")
    else:
        warn(f"{'arsenal command':24s} {Y}not on PATH in this shell{W}")

    print()
    if all_ok:
        info(f"{G}All required checks passed.{W}")
    else:
        warn(f"{Y}Some required checks failed — re-run without --check to install.{W}")
    return all_ok


# ─── post-install summary ────────────────────────────────────────────────────

def post_install_summary(os_type: str) -> None:
    step("Post-Install Verification Summary")
    results: list = []

    for tool in ("python3", "git"):
        path = shutil.which(tool)
        results.append((tool, bool(path), path or "missing"))

    if os_type not in ("macos", "windows"):
        path = shutil.which("fuser")
        results.append(("fuser (psmisc)", bool(path), path or "missing"))

    curses_ok = run_ok([sys.executable, "-c", "import curses"])
    results.append(("python:curses", curses_ok, "stdlib" if curses_ok else "missing"))

    docx_ok = run_ok([sys.executable, "-c", "import docx"])
    results.append(("python:docx", docx_ok,
                    "installed" if docx_ok else "missing"))

    arsenal_path = shutil.which("arsenal")
    results.append(("arsenal command", bool(arsenal_path),
                    arsenal_path or "not on PATH yet — open a new shell"))

    pad = max(len(r[0]) for r in results) + 2
    print()
    for label, ok_flag, detail in results:
        status_s = f"{G}OK{W}" if ok_flag else f"{R}FAIL{W}"
        print(f"  {label:<{pad}} {status_s}  {detail}")

    required_failures = [r for r in results if not r[1]]
    print()
    if not required_failures:
        info(f"{G}Arsenal installation complete.{W}")
        print()
        print(f"  {C}Run:{W}  sudo arsenal")
        print(f"  Arsenal manages tools in: /opt/pr0t31n/")
        print()
    else:
        warn(f"{Y}{len(required_failures)} required item(s) need attention (see above).{W}")


# ─── argument parser ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=f"Installer for {APP_NAME}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  sudo python3 install.py            # full install\n"
            "  sudo python3 install.py --check    # dependency check only\n"
            "  sudo python3 install.py --skip-apt # skip system packages\n"
        ),
    )
    p.add_argument("--skip-apt", "--skip-system", dest="skip_apt",
                   action="store_true", help="Skip system package installation")
    p.add_argument("--skip-pip",   action="store_true",
                   help="Skip pip package installation")
    p.add_argument("--skip-setup", action="store_true",
                   help="Skip arsenal command setup (symlink/PATH)")
    p.add_argument("--skip-go",    action="store_true",
                   help="(no-op) Arsenal has no Go dependencies")
    p.add_argument("--skip-git",   action="store_true",
                   help="(no-op) Arsenal has no git tool dependencies")
    p.add_argument("--check",      action="store_true",
                   help="Check dependencies without installing")
    return p.parse_args()


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    banner()
    args    = parse_args()
    os_type = detect_os()
    info(f"Detected OS type: {os_type}")

    if args.check:
        ok_flag = run_check(os_type)
        sys.exit(0 if ok_flag else 1)

    if os_type != "windows":
        require_root()

    # Phase 1 — system packages
    install_system_packages(os_type, args)

    # Phase 2 — pip (python-docx for Word reports; windows-curses on Windows)
    install_pip_packages(os_type, args)

    # Phase 3 — arsenal command setup
    script = APP_DIR / "arsenal.py"
    if not script.exists():
        err(f"arsenal.py not found at {script}")
        warn(f"Ensure this installer is located at {APP_DIR}/install.py")
        sys.exit(1)
    setup_arsenal_command(os_type, script, args)

    # Summary
    post_install_summary(os_type)


if __name__ == "__main__":
    main()
