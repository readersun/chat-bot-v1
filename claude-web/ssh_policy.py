#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ssh_policy
==========

**챗봇이 스스로 고른 명령**의 등급을 매긴다.

등급은 셋이다.

    read     조회. 승인 없이 실행한다.
    write    변경. 승인 카드가 뜨고, 사람이 누르기 전에는 나가지 않는다.
    blocked  보내지 않는다. 승인 단추도 만들지 않는다.

원칙
----
1. **모르는 명령은 write 다.** 화이트리스트에 없으면 승인을 받는다.
   읽기 목록은 "확실히 읽기만 하는 것" 만 담는다. 애매하면 넣지 않는다.
2. 한 줄에 여러 명령이 있으면(`;` `&&` `|` 줄바꿈) **가장 높은 등급**을 쓴다.
   `df -h && rm -rf /data` 가 조회로 통과하면 이 파일은 없는 것과 같다.
3. 리다이렉트(`>` `>>`)는 쓰기다. `cat a > b` 의 머리는 cat 이지만 파일을 만든다.
4. 명령 치환(`$(...)`, 백틱)이 있으면 안을 믿지 않고 write 로 올린다.
5. 비밀이 들어 있는 경로(/etc/shadow, id_rsa, .env ...)는 **조회도 막는다.**
   조회 등급은 승인 없이 나가기 때문에, 거기로 비밀을 읽어 오는 길이 열리면
   승인 절차 전체가 의미를 잃는다.
6. 대화형 명령(vi, top, less ...)은 막는다. 끝나지 않는 명령을 비대화형으로
   보내면 타임아웃까지 중계 한 자리를 잡고 있다가 아무 결과도 못 준다.
   사람이 직접 쓸 자리는 웹 터미널이다.

여기서 매기는 등급은 **사람이 터미널에서 직접 치는 줄에는 적용되지 않는다.**
브라우저가 PuTTY 를 대신하는 것일 뿐이고 그 사람의 계정 권한이 늘지 않는다.
"""

import re

READ = "read"
WRITE = "write"
BLOCKED = "blocked"

RANK = {READ: 1, WRITE: 2, BLOCKED: 3}

LEVEL_LABELS = {READ: "조회", WRITE: "변경", BLOCKED: "차단"}

MAX_COMMAND_CHARS = 2000

# ---------------------------------------------------------------------------
# 확실히 읽기만 하는 명령
#
# 여기에 하나를 더할 때마다 "이 명령이 파일을 만들거나 고치거나 지울 수 있는
# 인자가 있는가" 를 먼저 본다. 있으면 아래 _SUBCOMMANDS 나 _READ_GUARDS 에
# 조건을 함께 적는다.
# ---------------------------------------------------------------------------
READ_CMDS = {
    # 디스크 / 파일
    "ls", "ll", "dir", "df", "du", "stat", "file", "find", "locate", "readlink",
    "realpath", "basename", "dirname", "pwd", "tree", "lsblk", "blkid", "mount",
    "quota", "lsattr", "getfacl",
    # 내용 보기
    "cat", "tac", "head", "tail", "nl", "wc", "grep", "egrep", "fgrep", "zgrep",
    "zcat", "gunzip", "cut", "sort", "uniq", "tr", "sed", "awk", "diff", "cmp",
    "md5sum", "sha1sum", "sha256sum", "strings", "od", "xxd", "jq", "column",
    # 프로세스 / 자원
    "ps", "pgrep", "pidof", "free", "uptime", "vmstat", "iostat", "mpstat",
    "sar", "nproc", "lscpu", "lsmem", "lsof", "fuser", "dmesg", "numactl",
    # 시스템 / 신원
    "uname", "hostname", "hostnamectl", "whoami", "id", "groups", "who", "w",
    "last", "date", "timedatectl", "locale", "env", "printenv", "ulimit",
    "getent", "sestatus", "getenforce", "rpm", "dpkg", "lsb_release",
    # 네트워크 (상태 조회만)
    "ip", "ifconfig", "ss", "netstat", "route", "arp", "ping", "ping6",
    "traceroute", "tracepath", "dig", "nslookup", "host", "nc", "telnet",
    "curl", "wget", "openssl", "ssh-keygen",
    # 기타
    "echo", "printf", "true", "false", "which", "whereis", "type", "command",
    "systemctl", "service", "journalctl", "crontab", "docker", "podman",
    "kubectl", "git", "tar", "unzip", "zip", "ssh", "nvidia-smi", "smartctl",
    "ipmitool", "sensors",
}

# ---------------------------------------------------------------------------
# 확실히 쓰기/변경인 명령. (모르는 명령도 write 이므로 이 목록은 "설명용" 에
# 가깝다. 승인 카드의 이유 문구를 사람 말로 쓰기 위해 둔다)
# ---------------------------------------------------------------------------
WRITE_CMDS = {
    "rm", "rmdir", "mv", "cp", "install", "mkdir", "touch", "ln", "chmod",
    "chown", "chgrp", "setfacl", "chattr", "truncate", "tee", "dd", "rsync",
    "scp", "sftp", "sync", "gzip", "bzip2", "xz", "kill", "pkill", "killall",
    "renice", "nohup", "at", "apt", "apt-get", "yum", "dnf", "zypper", "pip",
    "pip3", "npm", "yarn", "make", "patch", "ldconfig", "update-alternatives",
    "useradd", "usermod", "groupadd", "groupmod", "chpasswd", "sudo", "su",
    "doas", "python", "python3", "perl", "ruby", "node", "bash", "sh", "zsh",
    "ksh", "eval", "exec", "source", "xargs", "mkswap", "swapon", "swapoff",
    "iptables", "nft", "firewall-cmd", "ufw", "setsebool", "semanage",
    "sysctl", "modprobe", "insmod", "rmmod", "mkdir", "logrotate",
}

# ---------------------------------------------------------------------------
# 절대 보내지 않는 것
#
# "승인받으면 되지 않나" 라고 물을 수 있다. 되지 않는다. 여기 있는 것들은
# 눌렀을 때 되돌릴 방법이 없거나(파티션/파일시스템), 승인 화면 자체를 끄거나
# (기록 지우기), 서버를 내려 승인한 사람도 확인할 수 없게 만든다.
# ---------------------------------------------------------------------------
_BLOCKED_PATTERNS = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*/\s*$", "루트를 지운다"),
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\*|\s|$)", "루트를 지운다"),
    (r"\bmkfs(\.|\s)", "파일시스템을 다시 만든다"),
    (r"\bwipefs\b", "파일시스템 서명을 지운다"),
    (r"\b(fdisk|parted|sfdisk|gdisk)\b", "파티션을 건드린다"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "장치에 직접 쓴다"),
    (r">\s*/dev/(sd|nvme|vd|hd|mapper)", "장치에 직접 쓴다"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "서버를 내린다"),
    (r"\binit\s+[06]\b", "서버를 내린다"),
    (r":\s*\(\s*\)\s*\{", "포크 폭탄"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*$", "루트 권한을 전부 연다"),
    (r"\buserdel\b", "계정을 지운다"),
    (r"\bpasswd\b(?!\s+-S)", "비밀번호를 바꾼다"),
    (r"\b(visudo|vipw)\b", "권한 파일을 대화형으로 고친다"),
    (r"\bhistory\s+-c\b", "기록을 지운다"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba)?sh\b", "받아서 바로 실행한다"),
    (r"\bnc\b[^|;&]*\s-e\b", "셸을 외부로 연다"),
    (r"\b(crontab|rm)\b[^|;&]*/var/log/audit", "감사 기록을 건드린다"),
    (r"\bauditctl\b", "감사 설정을 건드린다"),
    (r"\btruncate\b[^|;&]*/var/log", "로그를 비운다"),
    (r"\bsystemctl\s+(stop|disable|mask)\s+(auditd|rsyslog|sshd)", "기록/접속 수단을 끈다"),
]

BLOCKED_PATTERNS = [(re.compile(p, re.IGNORECASE), why) for p, why in _BLOCKED_PATTERNS]

# 비밀이 들어 있는 자리. 조회 등급은 승인 없이 나가므로 읽기도 막는다.
_SECRET_PATHS = [
    (r"/etc/shadow", "/etc/shadow"),
    (r"/etc/gshadow", "/etc/gshadow"),
    (r"/etc/sudoers", "/etc/sudoers"),
    (r"\bid_(rsa|dsa|ecdsa|ed25519)\b(?!\.pub)", "SSH 개인키"),
    (r"\.ssh/(?!known_hosts|config\b)", "SSH 설정 디렉터리"),
    (r"\.(pem|pfx|p12|jks|keystore)\b", "인증서 개인키"),
    (r"\.pgpass\b", "DB 접속 정보"),
    (r"\.my\.cnf\b", "DB 접속 정보"),
    (r"(^|[\s/])\.env(\.|\b)", ".env"),
    (r"\bcredentials?\b", "자격 증명 파일"),
    (r"\.aws/", "AWS 자격 증명"),
    (r"\bkrb5\.keytab\b", "커버로스 keytab"),
    (r"\bprivate[_-]?key\b", "개인키"),
]
SECRET_PATHS = [(re.compile(p, re.IGNORECASE), why) for p, why in _SECRET_PATHS]

# 끝나지 않는 명령. 비대화형으로 보내면 타임아웃까지 자리만 잡는다.
INTERACTIVE_CMDS = {
    "vi", "vim", "nvim", "nano", "emacs", "pico", "ed", "less", "more", "most",
    "top", "htop", "atop", "iotop", "iftop", "nmon", "watch", "man", "info",
    "mysql", "psql", "sqlite3", "redis-cli", "mongo", "ftp", "tmux", "screen",
    "python", "python3", "irb", "node", "bc", "systemd-run",
}

# 서브명령으로 읽기/쓰기가 갈리는 것들.
# 값: (읽기로 볼 서브명령 집합, 설명)
_SUBCOMMANDS = {
    "systemctl": ({"status", "show", "list-units", "list-unit-files", "cat",
                   "is-active", "is-enabled", "is-failed", "get-default",
                   "list-timers", "list-sockets", "list-dependencies"},
                  "서비스를 조작한다"),
    "service": (set(), "서비스를 조작한다"),          # service x status 는 아래에서 따로 본다
    "docker": ({"ps", "images", "logs", "inspect", "top", "stats", "version",
                "info", "port", "diff", "history", "events"},
               "컨테이너를 조작한다"),
    "podman": ({"ps", "images", "logs", "inspect", "top", "stats", "version",
                "info", "port", "diff", "history"},
               "컨테이너를 조작한다"),
    "kubectl": ({"get", "describe", "logs", "top", "explain", "api-resources",
                 "version", "cluster-info", "config"},
                "클러스터를 조작한다"),
    "git": ({"status", "log", "diff", "show", "branch", "remote", "config",
             "describe", "blame", "shortlog", "ls-files", "ls-remote",
             "rev-parse", "tag", "stash"},
            "저장소를 바꾼다"),
    "crontab": (set(), "예약 작업을 바꾼다"),         # crontab -l 은 아래에서 따로 본다
    "rpm": ({"-q", "-qa", "-qi", "-ql", "-qf", "-V"}, "패키지를 바꾼다"),
    "dpkg": ({"-l", "-L", "-s", "-S", "--list", "--status"}, "패키지를 바꾼다"),
}

# 읽기 명령이지만 특정 인자가 붙으면 쓰기가 되는 것들.
_READ_GUARDS = {
    "find": ({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls",
              "-fprint", "-fprintf"}, "파일을 지우거나 명령을 실행한다"),
    # 재귀 검색은 경로 금지 목록을 우회한다. `grep -r password /etc` 는
    # /etc/shadow 를 이름 없이 읽어 낸다. 그래서 훑기는 승인을 받는다.
    "grep": ({"-r", "-R", "--recursive", "--dereference-recursive"},
             "하위 전체를 훑는다"),
    "egrep": ({"-r", "-R", "--recursive"}, "하위 전체를 훑는다"),
    "fgrep": ({"-r", "-R", "--recursive"}, "하위 전체를 훑는다"),
    "sed": ({"-i", "--in-place"}, "파일을 직접 고친다"),
    "tar": ({"-x", "--extract", "-c", "--create", "-r", "-u", "--delete"},
            "파일을 풀거나 만든다"),
    "zip": ({""}, "압축 파일을 만든다"),
    "unzip": ({""}, "파일을 풀어 놓는다"),
    "gunzip": ({""}, "원본을 지우고 풀어 놓는다"),
    "openssl": ({"req", "genrsa", "genpkey", "rsa", "pkcs12", "enc"},
                "키나 인증서를 만든다"),
    "ssh-keygen": ({""}, "키를 만든다"),
    "curl": ({"-o", "-O", "--output", "--remote-name", "-T", "--upload-file",
              "-d", "--data", "-X", "--request"}, "파일을 내려받거나 올린다"),
    "wget": ({""}, "파일을 내려받는다"),
    "nc": ({""}, "임의의 연결을 연다"),
    "telnet": ({""}, "임의의 연결을 연다"),
    "ssh": ({""}, "다른 서버로 건너간다"),
    "env": ({"-i", "--ignore-environment"}, "환경을 바꿔 명령을 실행한다"),
    "ip": ({"add", "del", "set", "change", "replace", "flush"}, "네트워크를 바꾼다"),
    "mount": ({""}, "마운트를 바꾼다"),       # 인자 없는 mount 는 아래에서 조회로 본다
}


# ---------------------------------------------------------------------------
# 쪼개기
# ---------------------------------------------------------------------------
_SEPARATORS = (";", "&&", "||", "|", "\n", "&")


def split_segments(command):
    """
    한 줄을 명령 단위로 쪼갠다. 따옴표 안의 구분자는 구분자가 아니다.

    반환: [(조각 문자열, 리다이렉트 있었는지)]
    """
    out, buf = [], []
    redirect = False
    i, n = 0, len(command)
    quote = ""
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote and command[i - 1:i] != "\\":
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ">":
            # 2>&1 은 리다이렉트지만 파일을 만들지 않는다.
            tail = command[i:i + 4]
            if not re.match(r">&\d", tail):
                redirect = True
            buf.append(ch)
            i += 1
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            out.append(("".join(buf), redirect))
            buf, redirect = [], False
            i += 2
            continue
        if ch in (";", "|", "\n", "&"):
            out.append(("".join(buf), redirect))
            buf, redirect = [], False
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append(("".join(buf), redirect))
    return [(s.strip(), r) for s, r in out if s.strip() or r]


def _tokens(segment):
    """공백으로 나눈 토큰. 따옴표는 벗겨서 본다."""
    try:
        import shlex
        return shlex.split(segment, posix=True)
    except ValueError:
        # 따옴표가 안 닫힌 경우. 그대로 공백 분리해서 본다.
        return segment.split()


def _head(tokens):
    """
    실제 명령 이름. 앞의 환경변수 대입(FOO=1)과 경로를 걷어낸다.
    sudo / nohup 같은 앞잡이는 벗기되, 벗겼다는 사실을 함께 돌려준다.
    """
    wrappers = []
    for tok in tokens:
        if "=" in tok and not tok.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue                        # FOO=bar cmd
        name = tok.rsplit("/", 1)[-1].lower()
        if name in ("sudo", "doas", "su", "nohup", "nice", "ionice", "timeout",
                    "time", "stdbuf", "setsid", "env", "xargs"):
            wrappers.append(name)
            continue
        return name, wrappers
    return "", wrappers


# ---------------------------------------------------------------------------
# 등급 매기기
# ---------------------------------------------------------------------------
def _secret_hit(text):
    for rx, why in SECRET_PATHS:
        if rx.search(text):
            return why
    return None


def _blocked_hit(text):
    for rx, why in BLOCKED_PATTERNS:
        if rx.search(text):
            return why
    return None


def _classify_segment(segment, redirect):
    """한 조각의 (등급, 이유). 이유는 화면에 그대로 쓸 수 있는 한 문장이다."""
    text = segment.strip()
    if not text:
        # `cmd >` 처럼 조각이 비었는데 리다이렉트만 남은 경우.
        return (WRITE, "파일로 내보낸다(리다이렉트)") if redirect else (READ, "")

    why = _blocked_hit(text)
    if why:
        return BLOCKED, why

    tokens = _tokens(text)
    head, wrappers = _head(tokens)
    # _head 가 앞잡이(sudo, nohup ...)와 환경변수 대입을 벗겼으므로
    # 인자는 명령 이름이 나온 자리 다음부터 센다.
    args = []
    for i, t in enumerate(tokens):
        if t.rsplit("/", 1)[-1].lower() == head:
            args = tokens[i + 1:]
            break

    if head in INTERACTIVE_CMDS and head not in ("python", "python3", "node"):
        return BLOCKED, "끝나지 않는 대화형 명령이다. 터미널에서 직접 쓴다"

    secret = _secret_hit(text)
    if secret:
        return BLOCKED, "%s 는 읽지도 쓰지도 않는다" % secret

    if "$(" in text or "`" in text:
        return WRITE, "명령 치환이 들어 있어 안을 미리 알 수 없다"

    if redirect:
        return WRITE, "파일로 내보낸다(리다이렉트)"

    if wrappers and any(w in ("sudo", "doas", "su") for w in wrappers):
        return WRITE, "sudo 로 권한을 올린다"

    if not head:
        return WRITE, "명령을 알 수 없다"

    if head in WRITE_CMDS and head not in READ_CMDS:
        return WRITE, "%s 는 바꾸는 명령이다" % head

    if head in READ_CMDS:
        # 서브명령으로 갈리는 것
        if head in _SUBCOMMANDS:
            readable, note = _SUBCOMMANDS[head]
            sub = ""
            for a in args:
                if head in ("rpm", "dpkg"):
                    sub = a.lower()
                    break
                if not a.startswith("-"):
                    sub = a.lower()
                    break
            if head == "service":
                # service nfs status  <- 두 번째 인자가 동작이다
                acts = [a.lower() for a in args if not a.startswith("-")]
                if len(acts) >= 2 and acts[1] in ("status", "is-active"):
                    return READ, ""
                return WRITE, note
            if head == "crontab":
                if any(a in ("-l", "-list") for a in args):
                    return READ, ""
                return WRITE, note
            if sub and sub not in readable:
                return WRITE, note
            if not sub and head in ("docker", "podman", "kubectl", "git", "systemctl"):
                return READ, ""         # 인자 없는 호출은 도움말이다

        # 인자로 갈리는 것
        if head in _READ_GUARDS:
            flags, note = _READ_GUARDS[head]
            if "" in flags:
                if head == "mount" and not args:
                    return READ, ""     # 인자 없는 mount 는 목록이다
                return WRITE, note
            low = [a.lower() for a in args]
            for f in flags:
                if f in low or any(a.startswith(f + "=") for a in low):
                    return WRITE, note
                # 짧은 옵션은 붙여 쓴다. `tar -xzf`, `grep -rn`, `sed -i.bak`
                if len(f) == 2 and f.startswith("-"):
                    if any(a.startswith("-") and not a.startswith("--")
                           and f[1] in a[1:] for a in low):
                        return WRITE, note
        return READ, ""

    return WRITE, "%s 는 읽기 전용 목록에 없다" % head


def classify(command):
    """
    명령 하나(여러 줄일 수 있다)의 등급을 매긴다.

    반환 dict
        level   : read | write | blocked
        reason  : 사람에게 보여 줄 한 문장 (read 면 빈 문자열)
        parts   : [(조각, 등급, 이유)]  — 승인 카드에서 어느 조각이 걸렸는지 보여 준다
    """
    text = (command or "").strip()
    if not text:
        return {"level": BLOCKED, "reason": "빈 명령이다", "parts": []}
    if len(text) > MAX_COMMAND_CHARS:
        return {"level": BLOCKED,
                "reason": "명령이 너무 길다(%d자 / 최대 %d자)"
                          % (len(text), MAX_COMMAND_CHARS),
                "parts": []}
    if "\x00" in text:
        return {"level": BLOCKED, "reason": "명령에 들어갈 수 없는 문자가 있다", "parts": []}

    # 막는 무늬는 **쪼개기 전에** 한 번 본다. `curl ... | sh` 처럼 위험이
    # 파이프를 걸쳐 있는 경우가 있다. 조각으로 나눈 뒤에 보면 `curl ...` 과
    # `sh` 가 따로따로 평범해 보인다.
    whole = _blocked_hit(text)
    if whole:
        return {"level": BLOCKED, "reason": whole,
                "parts": [(text, BLOCKED, whole)]}

    parts, worst, reason = [], READ, ""
    for seg, redirect in split_segments(text):
        lv, why = _classify_segment(seg, redirect)
        parts.append((seg, lv, why))
        if RANK[lv] > RANK[worst]:
            worst, reason = lv, why
    return {"level": worst, "reason": reason, "parts": parts}


def needs_approval(level):
    return level == WRITE


# ---------------------------------------------------------------------------
# 결과 요약 (기록에 넣을 한 줄. 내용은 넣지 않는다)
# ---------------------------------------------------------------------------
def result_note(exit_code, output):
    text = output or ""
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return "종료 %s · %d줄 · %d바이트" % (
        "?" if exit_code is None else exit_code, lines, len(text.encode("utf-8", "replace")))


# ---------------------------------------------------------------------------
# 사람이 터미널에 친 줄을 기록할 때 가릴지 판단
#
# 등급은 매기지 않지만(사람의 권한 그대로다) 비밀번호 프롬프트 뒤에 온 줄을
# 그대로 적으면 기록 자체가 비밀 저장소가 된다. 직전 출력이 비밀번호를 묻고
# 있었으면 내용을 적지 않는다.
# ---------------------------------------------------------------------------
# `[sudo] password for svc_ops:` 처럼 묻는 말과 콜론 사이에 다른 글자가
# 끼는 것이 보통이다. "그 줄에 비밀번호라는 말이 있고 줄이 콜론으로 끝난다" 로
# 본다. 넉넉하게 잡는 쪽이 맞다. 잘못 가리면 기록 한 줄이 아쉬운 것으로
# 끝나지만, 못 가리면 비밀번호가 DB 에 남는다.
_PROMPT_RX = re.compile(
    r"(password|passphrase|비밀번호|암호)[^\n]*[:：?]\s*$", re.IGNORECASE)
_PROMPT_BARE_RX = re.compile(
    r"(password|passphrase|비밀번호|암호)\s*$", re.IGNORECASE)


def looks_like_password_prompt(tail):
    if not tail:
        return False
    last = tail.replace("\r", "\n").rstrip("\n").split("\n")[-1].strip()
    if not last:
        return False
    return bool(_PROMPT_RX.search(last) or _PROMPT_BARE_RX.search(last))
