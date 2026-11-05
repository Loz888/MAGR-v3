"""Drive an already prepared, private QEMU guest through real node fault checks.

No cloud instance is created. The guest has no NIC. Only the named QEMU process
is killed, and all boot/stage failures remain in the selected evidence directory.
"""

import argparse
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4


class Guest:
    def __init__(self, args):
        self.args = args
        self.process = None
        self.serial = None
        self.attached_pid = None
        self.sequence = 0
        self.buffer = b""

    def start(self):
        self.sequence += 1
        self.buffer = b""
        prefix = self.args.evidence / f"boot-{self.sequence}"
        serial = self.args.evidence / "serial.sock"
        qmp = self.args.evidence / "qmp.sock"
        for path in (serial, qmp):
            if path.exists():
                path.unlink()
        command = [
            str(self.args.tools / "usr/bin/qemu-system-x86_64"),
            "-machine",
            "pc,accel=tcg",
            "-cpu",
            "max",
            "-smp",
            "2",
            "-m",
            "3072",
            "-display",
            "none",
            "-L",
            str(self.args.tools / "usr/share/qemu"),
            "-vga",
            "none",
            "-nic",
            "none",
            "-no-reboot",
            "-bios",
            str(self.args.tools / "usr/share/seabios/bios-256k.bin"),
            "-kernel",
            str(self.args.kernel),
            "-initrd",
            str(self.args.initrd),
            "-append",
            "root=/dev/vda rw console=ttyS0 init=/mgar-bootstrap.sh apparmor=0",
            "-drive",
            f"file={self.args.image},format=raw,if=virtio,cache=writeback,discard=unmap",
            "-device",
            "virtio-rng-pci",
            "-fsdev",
            f"local,id=install,path={self.args.share},security_model=none,readonly=on",
            "-device",
            "virtio-9p-pci,fsdev=install,mount_tag=mgar-src",
            "-chardev",
            f"socket,id=console,path={serial},server=on,wait=off,logfile={prefix}.serial.log",
            "-serial",
            "chardev:console",
            "-qmp",
            f"unix:{qmp},server=on,wait=off",
        ]
        env = {**os.environ, "LD_LIBRARY_PATH": str(self.args.tools / "usr/lib/x86_64-linux-gnu")}
        # Provider bindings never enter the guest; also exclude them from QEMU's host process.
        for key in list(env):
            if any(word in key.upper() for word in ("TOKEN", "API_KEY", "PASSWORD", "SECRET")):
                env.pop(key)
        with prefix.with_suffix(".qemu.log").open("xb") as log:
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env
            )
        (self.args.evidence / "qemu.pid").write_text(str(self.process.pid))
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"VM boot {self.sequence} exited; retain qemu/serial logs")
            if serial.exists():
                self.serial = socket.socket(socket.AF_UNIX)
                self.serial.connect(str(serial))
                self.serial.settimeout(1)
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("Serial socket unavailable")
        while time.monotonic() < deadline:
            self.read()
            if b"root@mgar-r6-acceptance:" in self.buffer and self.buffer.rstrip().endswith(b"#"):
                print(
                    json.dumps({"boot": self.sequence, "console": "root_shell_ready"}), flush=True
                )
                return
            if self.process.poll() is not None:
                raise RuntimeError("Guest exited before real systemd console readiness")
        raise TimeoutError("Actual Linux boot readiness exceeded ten minutes")

    def read(self):
        try:
            b = self.serial.recv(65536)
            self.buffer += b
            return b
        except TimeoutError:
            return b""

    def stage(self, name):
        marker = uuid4().hex
        self.buffer = b""
        cmd = (
            "/opt/mgar/v3/.venv/bin/python "
            "/opt/mgar/v3/deploy/host-acceptance/guest_check.py "
            + shlex.quote(name)
            + f"; printf '\\n__MGAR_{marker}:%s\\n' \"$?\"\n"
        )
        self.serial.sendall(cmd.encode())
        deadline = time.monotonic() + 300
        pattern = re.compile(rb"__MGAR_" + marker.encode() + rb":([0-9]+)")
        while time.monotonic() < deadline:
            self.read()
            found = pattern.search(self.buffer)
            if found:
                path = self.args.evidence / f"boot-{self.sequence}-{name}.log"
                path.write_bytes(self.buffer)
                if found.group(1) != b"0":
                    raise RuntimeError(f"Guest stage {name} failed; retained {path.name}")
                answers = []
                clean = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.buffer)
                for line in clean.replace(b"\r", b"").splitlines():
                    if line.startswith(b"{"):
                        try:
                            answers.append(json.loads(line))
                        except ValueError:
                            pass
                result = next(r for r in answers if r.get("stage") == name)
                assert result["status"] == "PASS"
                print(json.dumps(result), flush=True)
                return result
        raise TimeoutError(f"Guest stage {name} timed out")

    def crash(self):
        if self.process is not None:
            assert self.process.poll() is None
            pid = self.process.pid
            os.kill(pid, signal.SIGKILL)
            self.process.wait(timeout=20)
            assert self.process.returncode == -signal.SIGKILL
        else:
            # Explicitly reviewed continuation may attach to a supervisor's orphan VM.
            pid = self.attached_pid
            assert pid and b"qemu-system-x86_64" in Path(f"/proc/{pid}/cmdline").read_bytes()
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                stat = Path(f"/proc/{pid}/stat")
                if not stat.exists() or stat.read_text().split()[2] == "Z":
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Attached VM did not stop after SIGKILL")
        self.serial.close()
        print(json.dumps({"boot": self.sequence, "whole_vm_sigkill": True, "pid": pid}), flush=True)

    def reboot(self):
        self.serial.sendall(b"systemctl reboot\n")
        self.wait_exit()
        self.serial.close()
        print(
            json.dumps({"boot": self.sequence, "normal_reboot_exit": self.process.returncode}),
            flush=True,
        )
        assert self.process.returncode == 0

    def wait_exit(self):
        # QEMU's connected serial backend can block guest shutdown when unread.
        deadline = time.monotonic() + 300
        while self.process.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("Guest normal shutdown exceeded five minutes")
            self.read()

    def poweroff(self):
        self.serial.sendall(b"systemctl poweroff\n")
        self.wait_exit()
        assert self.process.returncode == 0
        self.serial.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tools", type=Path, required=True)
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--kernel", type=Path, required=True)
    p.add_argument("--initrd", type=Path, required=True)
    p.add_argument("--share", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    args = p.parse_args()
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    guest = Guest(args)
    results = []
    try:
        guest.start()
        results.append(guest.stage("before"))
        results.append(guest.stage("prepare-cut"))
        guest.crash()
        guest.start()
        results.append(guest.stage("after-cut"))
        guest.reboot()
        guest.start()
        results.append(guest.stage("after-normal"))
        results.append(guest.stage("prepare-inflight"))
        guest.crash()
        guest.start()
        results.append(guest.stage("after-inflight"))
        results.append(guest.stage("backup-restore"))
        supplemental = guest.stage("runtime-assets")
        boot_ids = [
            r["checks"]["boot_id"]
            for r in results
            if r["stage"] in {"before", "after-cut", "after-normal", "after-inflight"}
        ]
        assert len(boot_ids) == 4 and len(set(boot_ids)) == 4
        guest.poweroff()
        (args.evidence / "result.json").write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "scope": "isolated_QEMU_Linux_systemd_test_node",
                    "stages": results,
                    "supplemental_stages": [supplemental],
                    "four_distinct_boot_ids": boot_ids,
                    "guest_stopped_after_acceptance": True,
                    "new_physical_model_requests": 0,
                    "physical_hypervisor_or_storage_power_loss_tested": False,
                },
                indent=2,
            )
            + "\n"
        )
        print(json.dumps({"status": "PASS", "guest_stopped_after_acceptance": True}), flush=True)
    except BaseException as e:
        (args.evidence / "failure.json").write_text(
            json.dumps(
                {
                    "status": "FAIL",
                    "completed_stages": results,
                    "error_type": type(e).__name__,
                    "reason": str(e),
                },
                indent=2,
            )
            + "\n"
        )
        raise


if __name__ == "__main__":
    main()
