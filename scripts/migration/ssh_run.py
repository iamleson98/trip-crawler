# -*- coding: utf-8 -*-
"""ssh_run.py — Run a command on the production server over SSH.

Usage: python3 ssh_run.py "command"
       python3 ssh_run.py --put LOCAL REMOTE
       python3 ssh_run.py --get REMOTE LOCAL
"""
import sys
from pathlib import Path

import paramiko

HOST = "169.58.249.26"
USER = "root"
KEY = Path.home() / ".ssh" / "id_ed25519"


def client():
    k = paramiko.Ed25519Key.from_private_key_file(str(KEY))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, pkey=k, timeout=20)
    return c


def main():
    args = sys.argv[1:]
    c = client()
    if args and args[0] == "--put":
        sftp = c.open_sftp()
        sftp.put(args[1], args[2])
        sftp.close()
        print(f"put {args[1]} -> {args[2]}")
    elif args and args[0] == "--get":
        sftp = c.open_sftp()
        sftp.get(args[1], args[2])
        sftp.close()
        print(f"get {args[1]} -> {args[2]}")
    else:
        cmd = " ".join(args)
        _, out, err = c.exec_command(cmd, timeout=280)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if o:
            print(o, end="" if o.endswith("\n") else "\n")
        if e:
            print("STDERR:", e, end="" if e.endswith("\n") else "\n")
        sys.exit(rc)
    c.close()


if __name__ == "__main__":
    main()
