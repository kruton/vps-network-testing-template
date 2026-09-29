import copy

import pytest
import yaml

from scripts.lab import network_config, topology


def test_third_segment_and_second_nic_are_generated(tmp_path):
    data = topology()
    expanded = copy.deepcopy(data)
    expanded["networks"]["partner"] = {
        "port": 9997,
        "ipv4": "203.0.113.0/24",
        "ipv6": "2001:db8:300::/64",
    }
    expanded["vms"]["router"]["networks"]["partner"] = {
        "ipv4": "203.0.113.1", "ipv6": "2001:db8:300::1"
    }
    expanded["vms"]["vps"]["networks"]["partner"] = {
        "ipv4": "203.0.113.10", "ipv6": "2001:db8:300::10"
    }
    path = tmp_path / "topology.yml"
    path.write_text(yaml.safe_dump(expanded))

    loaded = topology(path)
    vps_index = list(loaded["vms"]).index("vps") + 1
    config = network_config(loaded, vps_index, "vps")
    assert "lan3" in config["ethernets"]
    assert "203.0.113.10/24" in config["ethernets"]["lan3"]["addresses"]
    assert "2001:db8:300::10/64" in config["ethernets"]["lan3"]["addresses"]


def test_duplicate_switch_port_is_rejected(tmp_path):
    data = topology()
    data["networks"]["external"]["port"] = data["networks"]["internal"]["port"]
    path = tmp_path / "topology.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="switch port"):
        topology(path)


def test_ipv4_only_topology_generates_addresses_and_routes(tmp_path):
    data = topology()
    for network in data["networks"].values():
        del network["ipv6"]
    for vm in data["vms"].values():
        for ips in vm["networks"].values():
            del ips["ipv6"]
    path = tmp_path / "topology.yml"
    path.write_text(yaml.safe_dump(data))

    loaded = topology(path)
    config = network_config(loaded, list(loaded["vms"]).index("internal-client") + 1, "internal-client")
    lan = config["ethernets"]["lan1"]
    assert lan["addresses"] == ["192.0.2.100/24"]
    assert lan["routes"] == [{"to": "198.51.100.0/24", "via": "192.0.2.1"}]


def test_address_families_must_match_network(tmp_path):
    data = topology()
    del data["networks"]["external"]["ipv6"]
    path = tmp_path / "topology.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="address families must match"):
        topology(path)
