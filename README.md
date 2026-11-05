<details>
<summary><strong>FULL DISCLOSURE</strong> — AI Development & Legal Notice</summary>

---

### AI Development Disclosure

This tool is part of the **pr0t31n toolkit**, a collection of security testing and penetration testing automation tools. The toolkit was created through AI-assisted development, guided by the professional experience and domain expertise of a cybersecurity consultant and penetration tester. Ya, I named it after myself - but to be fair, AI is horrible at naming things.

**The pr0t31n toolkit exists to serve two purposes:**

1. **Automate repetitive penetration testing workflow steps.** Security assessments involve significant amounts of repetitive, procedural work — running sequential tool chains, parsing outputs, correlating findings across tools, and managing engagement data. Automating these steps frees testers to focus on the work that requires human judgment: manual testing, creative attack paths, analysis, and reporting.

2. **Demonstrate the benefits and failures of AI-assisted development.** This toolkit is a real-world case study in building production security tooling with AI assistance. It demonstrates both what AI-assisted development can accomplish and where it falls short. The quality of AI-generated code varies significantly — between context refreshes, the output can range from senior/principal-level software engineering to the work of a still-in-school intern. This inconsistency is itself part of the demonstration.

In the interest of transparency, every tool in the pr0t31n toolkit carries this disclosure. The creator believes that AI-assisted development should be acknowledged openly, not concealed. Users, reviewers, and anyone examining this code should know how it was built so they can evaluate it accordingly.

---

### Legal & Authorization Notice

> **⚠ WARNING: AUTHORIZED USE ONLY**

This tool is designed and intended **exclusively** for use in **authorized security testing engagements**. "Authorized" means **all** of the following conditions are met:

- **Explicit Written Permission.** You possess explicit, documented, written authorization from the owner or authorized representative of the target system(s), network(s), or application(s) to conduct security testing. Verbal agreements are not sufficient.
- **Defined Scope.** The authorization clearly defines the scope of testing, including which systems, networks, IP ranges, applications, and/or endpoints are included. You must not exceed the boundaries of this defined scope under any circumstances.
- **Defined Timeframe.** The authorization specifies the dates and times during which testing is permitted. Testing outside the authorized window is unauthorized testing.
- **Rules of Engagement.** You are operating under a set of rules of engagement (RoE) or equivalent governing document that establishes acceptable testing methods, escalation procedures, emergency contacts, and any restrictions or exclusions.
- **Legal Compliance.** Your testing complies with all applicable local, state, national, and international laws and regulations, including but not limited to the Computer Fraud and Abuse Act (CFAA), the General Data Protection Regulation (GDPR), and equivalent legislation in your jurisdiction.

**Prohibited uses** include but are not limited to: unauthorized access to or testing of any system; testing against systems without explicit written authorization; any activity that violates applicable laws; use for malicious purposes including data theft, extortion, sabotage, or disruption of services; distribution intended to facilitate unauthorized access; and use without a clear legal basis.

**THIS TOOL IS PROVIDED "AS IS," WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT.**

The creator(s) and contributor(s) accept no responsibility or liability for any damage, loss, legal consequence, or other harm resulting from the use or misuse of this tool — including damage to systems or data, legal penalties or prosecution, loss of data or revenue, and any direct, indirect, incidental, special, consequential, or punitive damages.

**By using this tool, you acknowledge that you are solely responsible for ensuring that your use is authorized, lawful, and within the defined scope of your engagement. You assume all risk associated with the use of this tool.**

Security testing is a professional discipline with significant ethical and legal obligations. Users operating in a professional capacity are expected to adhere to the standards and codes of conduct established by recognized industry bodies, including PTES, OWASP, NIST, and any certifying body under which they hold credentials.

---

</details>

# Arsenal

**Tool installer and manager for the pr0t31n toolkit** — a single-file curses TUI that checks, installs, updates, and uninstalls all ten pr0t31n security tools from their GitHub repositories into `/opt/pr0t31n/`.

Launches with a block ASCII banner, queries GitHub for the latest release of every tool, compares against installed versions, and drops into an interactive checkbox menu. Navigate the list with arrow keys, toggle installs and uninstalls with spacebar, and press execute. Arsenal clones, installs, starts services, and cleans up — no manual steps.

---


> **[View full documentation on the Codex Wiki](https://wiki.pr0t31n.com/tools/arsenal)** — detailed features, CLI reference, findings catalogue, and integration guides.

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
