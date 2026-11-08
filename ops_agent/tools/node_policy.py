#!/usr/bin/env python3
"""Allowlist of read-only diagnostic commands on the homelab nodes.

One file, two roles:
- the agent imports it and rejects a command before it ever opens SSH;
- deploy/node/install.sh copies it to each node as /usr/local/bin/ops-agent-ro, the forced command
  of the `ops-agent` user's SSH key. There it re-checks $SSH_ORIGINAL_COMMAND and runs the command
  without a shell. So even a modified agent, or a stolen key, can only run what is listed here.

Standard library only: it runs with the node's system python3.
Each command is validated argument by argument; anything not explicitly allowed is refused.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys

TIMEOUT = 20
MAX_OUTPUT = 64 * 1024
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

UNIT = re.compile(r"^[A-Za-z0-9@._:-]{1,128}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # host, container, interface
PATH = re.compile(r"^/[A-Za-z0-9._/-]{0,255}$")
TIME = re.compile(r"^[A-Za-z0-9 :.+-]{1,40}$")  # "1 hour ago", "2026-11-07 10:00", "yesterday"
PRIORITY = {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug", *map(str, range(8))}
DNS_TYPES = {"A", "AAAA", "CNAME", "MX", "NS", "PTR", "SOA", "SRV", "TXT", "ANY"}
CAT_FILES = {
    "/proc/loadavg", "/proc/meminfo", "/proc/swaps", "/proc/mdstat", "/proc/uptime", "/proc/vmstat",
    "/proc/pressure/cpu", "/proc/pressure/memory", "/proc/pressure/io",
    "/etc/os-release", "/etc/hosts", "/etc/resolv.conf",
}

# Shown to the model in the tool description, so keep it in sync with the validators below.
HELP = """\
overview                                  uptime, memory, disks, failed units in one call
uptime | hostname | timedatectl | w
uname [-a]
free [-h|-m|-w]
df [-h] [-i] [-T] [-l] [PATH]
lsblk [-f]
du -sh PATH | du -h -d 1 PATH             (as an unprivileged user: unreadable dirs are skipped)
cat FILE                                  only: """ + ", ".join(sorted(CAT_FILES)) + """
ps aux [--sort=-%cpu|--sort=-%mem]
top -bn1 [-o %CPU|%MEM]
sensors
systemctl status UNIT | is-active UNIT | is-failed UNIT | is-enabled UNIT
systemctl list-units [--failed] [--type=service|timer|mount|socket] | list-timers
journalctl [-u UNIT]... [-k] [-p PRIO] [-n N<=500] [--since TIME] [--until TIME] [-b [-N]] [-g REGEX]
ip [-br] [-s] [-4|-6] addr|route|link|neigh [show [dev IFACE]] | ip route get ADDR
ss [-tulnpas combined, e.g. -tlnp | -s]
ping -c N<=5 HOST
dig [@SERVER] NAME [TYPE] [+short]
docker ps [-a] | docker stats --no-stream | docker logs [--tail N<=500] [--since TIME] CONTAINER"""


# Fixed output formats, matched literally by the sudoers rule. No spaces (sudo-rs compares argument by
# argument, so a template with spaces never matches) and none of , : = \\ # (special in sudoers).
DOCKER_PS_FORMAT = "{{.Names}}|{{.Status}}|{{.Image}}"
DOCKER_STATS_FORMAT = "{{.Name}}|cpu|{{.CPUPerc}}|mem|{{.MemUsage}}|{{.MemPerc}}|net|{{.NetIO}}|disk|{{.BlockIO}}"


class PolicyError(ValueError):
    pass


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise PolicyError(msg)


def _match(regex: re.Pattern, value: str, what: str) -> str:
    _need(bool(regex.match(value)), f"invalid {what}: {value!r}")
    return value


def _int(value: str, lo: int, hi: int, what: str) -> str:
    _need(value.isdigit() and lo <= int(value) <= hi, f"{what} must be {lo}..{hi}")
    return value


def _flags(args: list[str], allowed: set[str], what: str) -> None:
    for a in args:
        _need(a in allowed, f"{what}: flag {a!r} not allowed (allowed: {' '.join(sorted(allowed))})")


def _no_args(cmd: str):
    def check(args: list[str]) -> list[str]:
        _need(not args, f"{cmd} takes no arguments here")
        return [cmd]
    return check


def _uname(args):
    _flags(args, {"-a", "-r", "-m"}, "uname")
    return ["uname", *args]


def _free(args):
    _flags(args, {"-h", "-m", "-g", "-w"}, "free")
    return ["free", *args]


def _df(args):
    flags = [a for a in args if a.startswith("-")]
    paths = [a for a in args if not a.startswith("-")]
    _flags(flags, {"-h", "-i", "-T", "-l", "-hT", "-hi", "-ih", "-Th"}, "df")
    _need(len(paths) <= 1, "df: at most one path")
    return ["df", *flags, *(_match(PATH, p, "path") for p in paths)]


def _lsblk(args):
    _flags(args, {"-f"}, "lsblk")
    return ["lsblk", *args]


def _du(args):
    _need(args[:1] in (["-sh"], ["-h"]), "du: use `du -sh PATH` or `du -h -d 1 PATH`")
    if args[0] == "-sh":
        _need(len(args) == 2, "du -sh takes one path")
        return ["du", "-xsh", _match(PATH, args[1], "path")]
    _need(len(args) == 4 and args[1] == "-d" and args[2] in ("1", "2"), "du: use `du -h -d 1 PATH`")
    return ["du", "-xh", "-d", args[2], _match(PATH, args[3], "path")]


def _cat(args):
    _need(len(args) == 1 and args[0] in CAT_FILES, f"cat: only {', '.join(sorted(CAT_FILES))}")
    return ["cat", args[0]]


def _ps(args):
    _need(args in (["aux"], ["aux", "--sort=-%cpu"], ["aux", "--sort=-%mem"]),
          "ps: use `ps aux [--sort=-%cpu|--sort=-%mem]`")
    return ["ps", *args]


def _top(args):
    _need(args in (["-bn1"], ["-bn1", "-o", "%CPU"], ["-bn1", "-o", "%MEM"]), "top: use `top -bn1 [-o %CPU|%MEM]`")
    return ["top", "-b", "-n", "1", "-w", "200", *args[1:]]


def _systemctl(args):
    _need(bool(args), "systemctl: subcommand required")
    sub, rest = args[0], args[1:]
    if sub in ("status", "is-active", "is-failed", "is-enabled"):
        _need(len(rest) == 1, f"systemctl {sub} takes one unit")
        unit = _match(UNIT, rest[0], "unit")
        if sub == "status":
            return ["systemctl", "status", "--no-pager", "--lines=20", "--", unit]
        return ["systemctl", sub, "--", unit]
    if sub == "list-units":
        _flags(rest, {"--failed", "--all", "--type=service", "--type=timer", "--type=mount", "--type=socket"},
               "systemctl list-units")
        return ["systemctl", "list-units", "--no-pager", "--plain", *rest]
    if sub == "list-timers":
        _need(not rest, "systemctl list-timers takes no arguments")
        return ["systemctl", "list-timers", "--no-pager", "--all"]
    raise PolicyError(f"systemctl {sub} not allowed")


def _journalctl(args):
    out = ["journalctl", "--no-pager", "-o", "short-iso"]
    has_n = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "-b":  # -b alone means the current boot; -b -1 the previous one
            if i < len(args) and re.fullmatch(r"-?[0-9]{1,2}", args[i]):
                out += ["-b", args[i]]
                i += 1
            else:
                out.append("-b")
        elif a in ("-u", "-p", "-n", "--since", "--until", "-g"):
            _need(i < len(args), f"journalctl {a} needs a value")
            value = args[i]
            i += 1
            if a == "-u":
                out += ["-u", _match(UNIT, value, "unit")]
            elif a == "-p":
                _need(value in PRIORITY or re.fullmatch(r"[a-z0-7]+\.\.[a-z0-7]+", value) is not None,
                      "journalctl -p: priority like err or warning")
                out += ["-p", value]
            elif a == "-n":
                out += ["-n", _int(value, 1, 500, "journalctl -n")]
                has_n = True
            elif a == "-g":
                _need(0 < len(value) <= 100 and "\n" not in value, "journalctl -g: pattern up to 100 chars")
                out += ["-g", value]
            else:
                out += [a, _match(TIME, value, "time")]
        elif a == "-k":
            out.append("-k")
        else:
            raise PolicyError(f"journalctl: argument {a!r} not allowed")
    return out if has_n else [*out, "-n", "200"]


def _ip(args):
    flags = []
    while args and args[0].startswith("-"):
        _flags([args[0]], {"-br", "-s", "-4", "-6", "-d"}, "ip")
        flags.append(args.pop(0))
    _need(bool(args), "ip: object required (addr, route, link, neigh)")
    obj, rest = args[0], args[1:]
    _need(obj in {"addr", "a", "address", "route", "r", "link", "l", "neigh", "n"}, f"ip {obj} not allowed")
    if obj in ("route", "r") and rest[:1] == ["get"]:
        _need(len(rest) == 2, "ip route get ADDR")
        return ["ip", *flags, "route", "get", _match(re.compile(r"^[0-9a-fA-F.:]{2,45}$"), rest[1], "address")]
    if rest[:1] == ["show"]:
        rest = rest[1:]
    if rest:
        _need(len(rest) == 2 and rest[0] == "dev", f"ip {obj}: only `show [dev IFACE]`")
        return ["ip", *flags, obj, "show", "dev", _match(NAME, rest[1], "interface")]
    return ["ip", *flags, obj, "show"]


def _ss(args):
    _need(len(args) <= 2 and all(re.fullmatch(r"-[tulnpasi]{1,8}", a) for a in args),
          "ss: only combined flags from -tulnpasi, e.g. -tlnp")
    return ["ss", *args]


def _ping(args):
    _need(len(args) == 3 and args[0] == "-c", "ping: use `ping -c N HOST`")
    return ["ping", "-c", _int(args[1], 1, 5, "ping -c"), "-W", "2", "--", _match(NAME, args[2], "host")]


def _dig(args):
    out = ["dig", "+time=2", "+tries=1"]
    names = 0
    for a in args:
        if a.startswith("@"):
            out.append("@" + _match(NAME, a[1:], "server"))
        elif a == "+short":
            out.append(a)
        elif a.upper() in DNS_TYPES:
            out.append(a.upper())
        else:
            _need(names == 0, "dig: one name")
            out.append(_match(NAME, a, "name"))
            names += 1
    _need(names == 1, "dig: name required")
    return out


def _docker(args):
    # docker talks to a root-owned socket, so these go through sudo; /etc/sudoers.d/ops-agent
    # (deploy/node/install.sh, generated from the formats above) allows exactly these `ps` and `stats`
    # lines and `docker logs --tail …`, nothing else. Never add the docker group: it is root-equivalent.
    sudo = ["sudo", "-n", "/usr/bin/docker"]
    _need(bool(args), "docker: subcommand required")
    sub, rest = args[0], args[1:]
    if sub == "ps":
        _need(rest in ([], ["-a"]), "docker ps [-a]")
        return [*sudo, "ps", "-a", "--format", DOCKER_PS_FORMAT]
    if sub == "stats":
        _need(rest in ([], ["--no-stream"]), "docker stats --no-stream")
        return [*sudo, "stats", "--no-stream", "--format", DOCKER_STATS_FORMAT]
    if sub == "logs":
        tail, since, name = "200", None, None
        it = iter(rest)
        for a in it:
            if a == "--tail":
                tail = _int(next(it, ""), 1, 500, "docker logs --tail")
            elif a == "--since":
                since = _match(re.compile(r"^[0-9]{1,4}[smh]$|^[0-9T:.Z-]{10,30}$"), next(it, ""), "since (e.g. 30m)")
            else:
                _need(name is None, "docker logs: one container")
                name = _match(NAME, a, "container")
        _need(name is not None, "docker logs: container required")
        return [*sudo, "logs", "--tail", tail, *(["--since", since] if since else []), "--", name]
    raise PolicyError(f"docker {sub} not allowed")


COMMANDS = {
    "uptime": _no_args("uptime"),
    "hostname": _no_args("hostname"),
    "timedatectl": _no_args("timedatectl"),
    "w": _no_args("w"),
    "sensors": _no_args("sensors"),
    "uname": _uname,
    "free": _free,
    "df": _df,
    "lsblk": _lsblk,
    "du": _du,
    "cat": _cat,
    "ps": _ps,
    "top": _top,
    "systemctl": _systemctl,
    "journalctl": _journalctl,
    "ip": _ip,
    "ss": _ss,
    "ping": _ping,
    "dig": _dig,
    "docker": _docker,
}

OVERVIEW = [["uptime"], ["free", "-h"], ["df", "-hT", "-l"], ["systemctl", "list-units", "--failed"]]


def parse(command: str) -> list[str]:
    _need(len(command) <= 300, "command too long")
    _need(not re.search(r"[;&|`$<>\n\\]", command), "shell syntax (; & | ` $ < > \\ newline) is not allowed")
    try:
        return shlex.split(command)
    except ValueError as e:
        raise PolicyError(f"cannot parse: {e}") from None


def validate(argv: list[str]) -> list[list[str]]:
    """Return the exact argv list(s) to run, or raise PolicyError."""
    _need(bool(argv), "empty command")
    cmd, args = argv[0], list(argv[1:])
    if cmd == "overview":
        _need(not args, "overview takes no arguments")
        return [validate(c)[0] for c in OVERVIEW]
    _need(cmd in COMMANDS, f"{cmd!r} is not an allowed command. Allowed:\n{HELP}")
    return [COMMANDS[cmd](args)]


def run(argvs: list[list[str]]) -> int:
    env = {"PATH": SAFE_PATH, "LC_ALL": "C.UTF-8", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0", "TERM": "dumb"}
    status = 0
    for argv in argvs:
        exe = shutil.which(argv[0], path=SAFE_PATH)
        if exe is None:
            print(f"$ {shlex.join(argv)}\n{argv[0]}: not installed", flush=True)
            status = 127
            continue
        if len(argvs) > 1:
            print(f"$ {shlex.join(argv)}", flush=True)
        try:
            p = subprocess.run([exe, *argv[1:]], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=TIMEOUT)
            out = (p.stdout + p.stderr)[:MAX_OUTPUT]
            sys.stdout.write(out.decode("utf-8", "replace"))
            if p.returncode:
                print(f"(exit {p.returncode})")
                status = p.returncode
        except subprocess.TimeoutExpired:
            print(f"(timed out after {TIMEOUT}s)")
            status = 124
        sys.stdout.flush()
    return status


def main() -> int:
    """Forced-command entry point on the node."""
    command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        argvs = validate(parse(command))
    except PolicyError as e:
        print(f"refused: {e}")
        return 126
    return run(argvs)


if __name__ == "__main__":
    sys.exit(main())
