"""Assertions use independent expectations and real guest-to-guest traffic."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TOPOLOGY = yaml.safe_load((ROOT / "topology.yml").read_text())
EXPECTATIONS = yaml.safe_load((ROOT / "expectations.yml").read_text())["probes"]


def guest(name: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    port = TOPOLOGY["vms"][name]["ssh_port"]
    return subprocess.run(
        [
            "ssh", "-F", "/dev/null", "-i", str(ROOT / ".lab/ssh_key"),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-p", str(port),
            "lab@127.0.0.1", *argv,
        ],
        capture_output=True, text=True, timeout=20, check=True,
    )


def probe(source: str, destination: str, network: str, protocol: str, port: int, family: int) -> dict:
    key = f"ipv{family}"
    source_ip = TOPOLOGY["vms"][source]["networks"][network][key]
    dest_ip = TOPOLOGY["vms"][destination]["networks"][network][key]
    result = guest(source, [
        "python3", "/usr/local/bin/lab-probe.py",
        "--family", str(family), "--protocol", protocol,
        "--source", source_ip, "--destination", dest_ip, "--port", str(port),
    ])
    return json.loads(result.stdout)


def require_family(family: int, *networks: str) -> None:
    key = f"ipv{family}"
    if any(key not in TOPOLOGY["networks"][network] for network in networks):
        pytest.skip(f"IPv{family} is not configured on these networks")


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("expectation", EXPECTATIONS)
def test_firewall_matrix(expectation, family):
    source = expectation["source"]
    network = expectation["network"]
    require_family(family, network)
    result = probe(
        source, expectation["destination"], network,
        expectation["protocol"], expectation["port"], family,
    )
    interface_index = list(TOPOLOGY["vms"][source]["networks"]).index(network) + 1
    assert result["dev"] == f"eth{interface_index}", result
    assert result["via"] is None, result  # same segment, never the management NIC
    assert result["reply"] is expectation["allowed"], result


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("source,destination,network", [
    ("internal-client", "external-client", "internal"),
    ("external-client", "internal-client", "external"),
])
def test_router_blocks_cross_segment_traffic(source, destination, network, family):
    other_network = next(iter(TOPOLOGY["vms"][destination]["networks"]))
    require_family(family, network, other_network)
    key = f"ipv{family}"
    source_ip = TOPOLOGY["vms"][source]["networks"][network][key]
    dest_ip = TOPOLOGY["vms"][destination]["networks"][other_network][key]
    result = guest(source, [
        "python3", "/usr/local/bin/lab-probe.py",
        "--family", str(family), "--protocol", "tcp", "--source", source_ip,
        "--destination", dest_ip, "--port", "7777",
    ])
    outcome = json.loads(result.stdout)
    assert outcome["via"] == TOPOLOGY["vms"]["router"]["networks"][network][key]
    assert not outcome["reply"], outcome


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("protocol,port", [("tcp", 8080), ("udp", 8081), ("tcp", 9090), ("udp", 9091)])
def test_vps_has_real_listeners(protocol, port, family):
    require_family(family, "external")
    key = f"ipv{family}"
    destination = TOPOLOGY["vms"]["vps"]["networks"]["external"][key]
    result = guest("vps", [
        "python3", "/usr/local/bin/lab-probe.py",
        "--family", str(family), "--protocol", protocol,
        "--source", destination, "--destination", destination, "--port", str(port),
    ])
    assert json.loads(result.stdout)["reply"]
