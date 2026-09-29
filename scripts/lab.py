#!/usr/bin/env python3
"""Build and run an isolated QEMU/vswitch lab from topology.yml."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".lab"
RUN = STATE / "run"
IMAGE_NAME = "alpine-3.23.6-x86_64-bios-cloudinit-r0.qcow2"
IMAGE_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/" + IMAGE_NAME
)


def command(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, **kwargs)


def topology(path: Path = ROOT / "topology.yml") -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("topology must be a mapping")
    networks, vms = data.get("networks"), data.get("vms")
    if not isinstance(networks, dict) or not networks:
        raise ValueError("networks must be a nonempty mapping")
    if not isinstance(vms, dict) or not vms:
        raise ValueError("vms must be a nonempty mapping")

    ports, ssh_ports, addresses = set(), set(), set()
    subnets = {}
    for name, network in networks.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ValueError(f"invalid network name: {name}")
        if not isinstance(network, dict):
            raise ValueError(f"{name}: network must be a mapping")
        port = network.get("port")
        if not isinstance(port, int) or not 1024 <= port <= 65535 or port in ports:
            raise ValueError(f"{name}: duplicate or invalid switch port")
        ports.add(port)
        subnets[name] = {}
        for family, version in (("ipv4", 4), ("ipv6", 6)):
            if family == "ipv6" and family not in network:
                continue
            subnet = ipaddress.ip_network(network[family], strict=True)
            if subnet.version != version:
                raise ValueError(f"{name}: {family} has the wrong address family")
            for other in subnets.values():
                if family in other and subnet.overlaps(other[family]):
                    raise ValueError(f"{name}: overlapping {family} network")
            subnets[name][family] = subnet

    for index, (name, vm) in enumerate(vms.items(), 1):
        if index > 250 or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ValueError(f"invalid VM name: {name}")
        if not isinstance(vm, dict) or not isinstance(vm.get("networks"), dict):
            raise ValueError(f"{name}: networks must be a mapping")
        ssh_port = vm.get("ssh_port")
        if not isinstance(ssh_port, int) or not 1024 <= ssh_port <= 65535:
            raise ValueError(f"{name}: invalid SSH port")
        if ssh_port in ssh_ports or ssh_port in ports:
            raise ValueError(f"{name}: duplicate SSH or switch port")
        ssh_ports.add(ssh_port)
        if len(vm["networks"]) > 250:
            raise ValueError(f"{name}: too many NICs")
        for segment, ips in vm["networks"].items():
            if segment not in networks or not isinstance(ips, dict):
                raise ValueError(f"{name}: unknown network {segment}")
            if set(ips) != set(subnets[segment]):
                raise ValueError(f"{name}: address families must match {segment}")
            for family, version in (("ipv4", 4), ("ipv6", 6)):
                if family not in subnets[segment]:
                    continue
                address = ipaddress.ip_address(ips[family])
                subnet = subnets[segment][family]
                if address.version != version or address not in subnet:
                    raise ValueError(f"{name}: {address} outside {segment} {family}")
                if str(address) in addresses or address == subnet.network_address:
                    raise ValueError(f"{name}: duplicate or reserved address {address}")
                if version == 4 and address == subnet.broadcast_address:
                    raise ValueError(f"{name}: broadcast address {address}")
                addresses.add(str(address))

    if "router" not in vms or not vms["router"]["networks"].keys() >= networks.keys():
        raise ValueError("router must have a NIC on every network")
    return data


def mac(vm_index: int, nic_index: int) -> str:
    return f"52:54:00:ab:{vm_index:02x}:{nic_index:02x}"


def network_config(data: dict, vm_index: int, name: str) -> dict:
    vm = data["vms"][name]
    ethernets = {
        "mgmt": {
            "match": {"macaddress": mac(vm_index, 0)},
            "set-name": "eth0",
            "dhcp4": True,
            "dhcp6": False,
        }
    }
    for nic_index, (segment, ips) in enumerate(vm["networks"].items(), 1):
        net = data["networks"][segment]
        iface = {
            "match": {"macaddress": mac(vm_index, nic_index)},
            "set-name": f"eth{nic_index}",
            "dhcp4": False,
            "dhcp6": False,
            "addresses": [
                f"{ips[family]}/{ipaddress.ip_network(net[family]).prefixlen}"
                for family in ("ipv4", "ipv6") if family in net
            ],
        }
        # Routes to other lab segments always traverse the test router. The
        # management NIC remains available only for SSH and bootstrapping.
        if name != "router":
            iface["routes"] = []
            for other, prefix in data["networks"].items():
                if other in vm["networks"]:
                    continue
                gateway = data["vms"]["router"]["networks"][segment]
                for family in ("ipv4", "ipv6"):
                    if family not in prefix or family not in gateway:
                        continue
                    iface["routes"].append(
                        {"to": prefix[family], "via": gateway[family]}
                    )
        ethernets[f"lan{nic_index}"] = iface
    return {"version": 2, "ethernets": ethernets}


def user_data(pubkey: str, name: str) -> str:
    # Alpine's sshd rejects public keys for a locked account. Give the user an
    # unguessable, unused password hash and keep SSH password auth disabled.
    password_hash = command(
        "openssl", "passwd", "-6", "-stdin",
        input=secrets.token_urlsafe(48), capture_output=True,
    ).stdout.strip()
    config = {
        "hostname": name,
        "ssh_pwauth": False,
        "users": [
            "default",
            {
                "name": "lab",
                "groups": ["wheel"],
                "shell": "/bin/sh",
                "passwd": password_hash,
                "lock_passwd": False,
                "ssh_authorized_keys": [pubkey.strip()],
            },
        ],
        "runcmd": ["resize2fs /dev/vda"],
    }
    return "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)


def download_image() -> Path:
    image = STATE / "images" / IMAGE_NAME
    image.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(IMAGE_URL + ".sha512", timeout=60) as response:
        digest = response.read().decode().strip().split()[0]
    if not re.fullmatch(r"[0-9a-fA-F]{128}", digest):
        raise RuntimeError("invalid Alpine SHA-512 sidecar")

    def valid(path: Path) -> bool:
        if not path.exists():
            return False
        hasher = hashlib.sha512()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest().lower() == digest.lower()

    if not valid(image):
        partial = image.with_suffix(".partial")
        with urllib.request.urlopen(IMAGE_URL, timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if not valid(partial):
            partial.unlink(missing_ok=True)
            raise RuntimeError("Alpine image checksum mismatch")
        partial.replace(image)
    return image


def ssh_args(port: int) -> list[str]:
    return [
        "ssh", "-F", "/dev/null", "-i", str(STATE / "ssh_key"),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=3",
        "-p", str(port), "lab@127.0.0.1",
    ]


def wait_for_ssh(data: dict, timeout: int = 600) -> None:
    pending = set(data["vms"])
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for name in list(pending):
            port = data["vms"][name]["ssh_port"]
            try:
                result = subprocess.run(
                    ssh_args(port) + ["cloud-init status"],
                    capture_output=True, text=True, timeout=8,
                )
            except subprocess.TimeoutExpired:
                continue
            if "status: done" in result.stdout:
                pending.remove(name)
                print(f"ready: {name}", flush=True)
            elif "status: error" in result.stdout:
                raise RuntimeError(f"{name}: cloud-init failed: {result.stdout}")
        if pending:
            time.sleep(3)
    if pending:
        raise TimeoutError(f"VMs did not become ready: {', '.join(sorted(pending))}")


def inventory(data: dict) -> None:
    key = STATE / "ssh_key"
    connection_vars = {
        "ansible_user": "lab",
        "ansible_become": True,
        "ansible_become_method": "doas",
        "ansible_python_interpreter": "/usr/bin/python3",
        "ansible_ssh_private_key_file": str(key),
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    }
    hosts = {}
    for name, vm in data["vms"].items():
        hosts[name] = {
            "ansible_host": "127.0.0.1",
            "ansible_port": vm["ssh_port"],
            "lab_interfaces": [
                {"network": segment, "interface": f"eth{index}", **ips}
                for index, (segment, ips) in enumerate(vm["networks"].items(), 1)
            ],
        }
    groups = {
        "lab_vms": {"hosts": hosts, "vars": connection_vars},
        "routers": {"hosts": {"router": {}}},
        "targets": {"hosts": {"vps": {}} if "vps" in hosts else {}},
        "clients": {"hosts": {name: {} for name in hosts if name.endswith("client")}},
    }
    inventory_data = {"all": {"children": groups}}
    (STATE / "inventory.yml").write_text(yaml.safe_dump(inventory_data, sort_keys=False))


def up() -> None:
    data = topology()
    if RUN.exists():
        raise RuntimeError("lab already exists; run 'python scripts/lab.py down' first")
    shutil.rmtree(STATE / "logs", ignore_errors=True)
    RUN.mkdir(parents=True)
    try:
        image = download_image()
        key = STATE / "ssh_key"
        if not key.exists():
            command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vps-network-lab", "-f", str(key))
        pubkey = key.with_suffix(".pub").read_text()
        inventory(data)

        ports = ",".join(str(net["port"]) for net in data["networks"].values())
        command(
            "vswitch", "-daemon", "-ports", ports,
            "-pid-file", str(RUN / "vswitch.pid"),
            "-log-file", str(RUN / "vswitch.log"),
        )
        for vm_index, (name, vm) in enumerate(data["vms"].items(), 1):
            directory = RUN / name
            directory.mkdir()
            (directory / "user-data").write_text(user_data(pubkey, name))
            (directory / "meta-data").write_text(f"instance-id: {name}\nlocal-hostname: {name}\n")
            (directory / "network-config").write_text(
                yaml.safe_dump(network_config(data, vm_index, name), sort_keys=False)
            )
            command(
                "genisoimage", "-quiet", "-output", str(directory / "seed.iso"),
                "-volid", "cidata", "-joliet", "-rock",
                "user-data", "meta-data", "network-config", cwd=directory,
            )
            command(
                "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b",
                str(image), str(directory / "disk.qcow2"),
                stdout=subprocess.DEVNULL,
            )
            command("qemu-img", "resize", "-f", "qcow2", str(directory / "disk.qcow2"), "2G", stdout=subprocess.DEVNULL)
            acceleration = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
            args = [
                "qemu-system-x86_64", "-name", f"lab-{name}", "-m", "512", "-smp", "1",
                "-machine", "q35", "-accel", acceleration,
                "-display", "none", "-monitor", "none", "-serial", f"file:{directory / 'console.log'}",
                "-drive", f"file={directory / 'disk.qcow2'},if=virtio,format=qcow2",
                "-drive", f"file={directory / 'seed.iso'},media=cdrom,format=raw,readonly=on",
                "-netdev", f"user,id=mgmt,hostfwd=tcp:127.0.0.1:{vm['ssh_port']}-:22",
                "-device", f"virtio-net-pci,netdev=mgmt,mac={mac(vm_index, 0)}",
            ]
            for nic_index, segment in enumerate(vm["networks"], 1):
                port = data["networks"][segment]["port"]
                args += [
                    "-netdev", f"socket,id=lan{nic_index},connect=127.0.0.1:{port}",
                    "-device", f"virtio-net-pci,netdev=lan{nic_index},mac={mac(vm_index, nic_index)}",
                ]
            with (directory / "qemu.log").open("w") as log:
                process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            time.sleep(1)
            if process.poll() is not None and acceleration == "kvm":
                args[args.index("kvm")] = "tcg"
                with (directory / "qemu.log").open("a") as log:
                    log.write("KVM unavailable; retrying with software emulation\n")
                    process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                time.sleep(1)
            if process.poll() is not None:
                raise RuntimeError(f"{name}: QEMU exited during startup; see {directory / 'qemu.log'}")
            (directory / "qemu.pid").write_text(str(process.pid))
        wait_for_ssh(data)
    except Exception:
        down()
        raise


def down() -> None:
    if RUN.exists():
        logs = STATE / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        for log in RUN.glob("*/[cq]*.log"):
            shutil.copy2(log, logs / f"{log.parent.name}-{log.name}")
        switch_log = RUN / "vswitch.log"
        if switch_log.exists():
            shutil.copy2(switch_log, logs / "vswitch.log")
        for pidfile in RUN.glob("*/qemu.pid"):
            try:
                pid = int(pidfile.read_text())
                os.killpg(pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        time.sleep(2)
        pidfile = RUN / "vswitch.pid"
        if pidfile.exists():
            subprocess.run(
                ["vswitch", "-stop", "-pid-file", str(pidfile)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        shutil.rmtree(RUN)
    (STATE / "inventory.yml").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "up", "down", "status"])
    action = parser.parse_args().action
    if action == "check":
        data = topology()
        print(f"valid: {len(data['networks'])} networks, {len(data['vms'])} VMs")
    elif action == "up":
        up()
    elif action == "down":
        down()
    else:
        for pidfile in RUN.glob("*/qemu.pid"):
            print(f"{pidfile.parent.name}: PID {pidfile.read_text().strip()}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, TimeoutError, OSError, subprocess.CalledProcessError) as error:
        print(f"lab: {error}", file=sys.stderr)
        sys.exit(1)
