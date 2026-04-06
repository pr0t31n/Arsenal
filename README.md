# Arsenal

**Tool installer and manager for the pr0t31n toolkit** — a single-file curses TUI that checks, installs, updates, and uninstalls all ten pr0t31n security tools from their GitHub repositories into `/opt/pr0t31n/`.

Launches with a block ASCII banner, queries GitHub for the latest release of every tool, compares against installed versions, and drops into an interactive checkbox menu. Navigate the list with arrow keys, toggle installs and uninstalls with spacebar, and press execute. Arsenal clones, installs, starts services, and cleans up — no manual steps.

---

## Features

- **Live status dashboard** — shows each app's installed version, latest GitHub version, LAN port, and service state (systemd / launchd) before any interaction
- **Update detection** — highlights outdated tools in yellow; offers a single `y/n` prompt to `git pull` and reinstall all of them at once
- **curses selection menu** — scrollable checkbox list; `[✓]` installed, `[+]` marked to install, `[-]` marked to uninstall, `[ ]` not installed; right-edge scrollbar; minimum-size guard
- **Dependency auto-resolution** — selecting Spectre auto-adds Aether; Gauntlet auto-adds Gearbox; Vantage auto-adds Scattershot3 and OSift; topological install order guaranteed
- **Install** — `git clone --depth=1` into `/opt/pr0t31n/<Repo>/`, then runs the repo's own `install.py` (pip packages, system tools, boot service or symlinks)
- **Uninstall** — stops and disables the systemd unit / launchd plist, removes the service/plist file, cleans `/usr/local/bin` symlinks, and deletes the install directory
- **Boot service management** — Flask apps (Quiver, Gauntlet, Spectre, Vantage) are registered as persistent services; Arsenal's uninstaller tears them down cleanly
- **stdlib only** — `curses`, `json`, `os`, `platform`, `re`, `shutil`, `subprocess`, `sys`, `urllib`; no pip dependencies

---

## Requirements

- Python 3.9+
- Linux or macOS
- Root / sudo privileges

```bash
sudo python3 arsenal.py
```

---

## Managed Tools

| Tool | Port | Service | Requires | Description |
|------|------|---------|---------|-------------|
| [Quiver](https://github.com/pr0t31n/Quiver) | 5002 | `pr0t31n-quiver` | — | Hardware hacking command centre — SSH, serial, and WebSocket shells for Hak5 and offensive security hardware |
| [Aether](https://github.com/pr0t31n/Aether) | — | — | — | Five-phase wireless pentest orchestrator — WPA/WPA2/WPA3, WPA-Enterprise, Dragonblood, KRACK |
| [Spectre](https://github.com/pr0t31n/Spectre) | 5300 | `pr0t31n-spectre` | Aether | Web dashboard for Aether — real-time phase tracking and findings streaming |
| [Gearbox](https://github.com/pr0t31n/Gearbox) | — | — | — | Hardware security assessment CLI — nmap, Nessus, sslscan, nuclei, Responder, and 20+ tools |
| [Gauntlet](https://github.com/pr0t31n/Gauntlet) | 5000 | `pr0t31n-gauntlet` | Gearbox | Web platform for Gearbox — session management, live findings, assessment dashboard |
| [Pivot](https://github.com/pr0t31n/Pivot) | — | — | — | mTLS SOCKS5 relay daemon — asyncio agent/client for network pivoting with iptables transparent proxy |
| [Vantage](https://github.com/pr0t31n/Vantage) | 5100 | `pr0t31n-vantage` | Scattershot3, OSift | Recon platform — unified dashboard for network pentest and OSINT with live findings |
| [OSift](https://github.com/pr0t31n/OSift) | — | — | — | 19-phase OSINT framework — DNS, breach intel, GitHub secrets, cloud assets, M365 enumeration |
| [Scattershot3](https://github.com/pr0t31n/Scattershot3) | — | — | — | 13-phase network pentest recon — host discovery, port scan, TLS, AD/Kerberos, web app testing |
| [Mole](https://github.com/pr0t31n/Mole) | — | — | — | Go SOCKS5 + DNS proxy — iptables/pf transparent relay for DNS interception and tunnelling |

---

## Usage

```bash
sudo python3 arsenal.py
```

Arsenal requires root. Running without `sudo` prints an error and exits immediately.

## Version Detection

**Local version** — Arsenal greps the installed app's main source file for:
- Python: `VERSION = "x.y.z"`
- Go: `var version = "x.y.z"`

**GitHub version** — GitHub REST API, unauthenticated:
1. `GET /repos/pr0t31n/<Repo>/releases/latest` → `tag_name`
2. Fallback: `GET /repos/pr0t31n/<Repo>/tags` → first tag

Anonymous API access is rate-limited to 60 requests per hour. Arsenal makes up to 20 requests per run (2 per tool). Add a `GITHUB_TOKEN` environment variable to raise the limit to 5,000/hour if needed.

---

## Contributors

Arsenal is a standalone tool with no external Python dependencies. 
