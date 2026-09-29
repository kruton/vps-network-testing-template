# VPS network test template

This template boots four Alpine VMs on two isolated, dual-stack Ethernet
segments. A router and a VPS have NICs on both segments. Internal and external
clients send real TCP and UDP traffic to the VPS, and pytest checks the
observed result against [expectations.yml](expectations.yml). The external
segment acts as a **simulated Internet**; it does not expose the guests to real
incoming Internet traffic.

```
internal (192.0.2.0/24, 2001:db8:100::/64)
  internal-client ──┬── vps ──┬── external-client
                   router   external (198.51.100.0/24, 2001:db8:200::/64)
```

Each segment maps to one `vswitch` listening port. QEMU socket NICs connect VMs
to those ports. Each VM also has a separate QEMU user-mode NIC for SSH and
package installation; the tests bind their source IP and check the selected
route so that management traffic cannot satisfy a network assertion.  `vswitch`
listens on the host's configured TCP ports, so run this lab on a trusted host
or restrict those ports to local connections with the host firewall.

## Quick start

Use a Linux x86_64 host with QEMU (`qemu-system-x86_64` and `qemu-img`),
`genisoimage`, OpenSSH client, and [mise](https://mise.jdx.dev/). On Ubuntu,
install `qemu-system-x86 qemu-utils genisoimage openssh-client`.  At least 4
GiB free RAM and several GiB of disk space are recommended. KVM improves speed;
the lab falls back to QEMU software emulation without it.

```sh
mise install
mise exec -- uv sync --locked
mise exec -- uv run ansible-galaxy collection install -r requirements.yml
mise exec -- uv run molecule test
```

The first run downloads a checksum-verified Alpine cloud image into
`.lab/images`.  VM disks, generated SSH keys, inventory, and logs remain under
`.lab/` and are ignored by Git. `molecule test` destroys the VMs after
verification. For interactive debugging:

```sh
mise exec -- uv run molecule create
mise exec -- uv run molecule converge
mise exec -- uv run molecule verify
mise exec -- uv run molecule destroy
```

`mise exec -- uv run python scripts/lab.py status` shows VM PIDs. SSH to a VM
with `ssh -i .lab/ssh_key -p 2222 lab@127.0.0.1` (VPS example). The key is
generated locally. All forwarded SSH ports bind to `127.0.0.1`.

## Customize the template

1. Edit [topology.yml](topology.yml) to add segments, VMs, and NIC addresses.
   Each segment needs a unique switch port and IPv4 prefix. IPv6 is optional:
   omit `ipv6` from a segment and from every VM address on that segment for an
   IPv4-only lab. Add the router to every segment. The generator creates
   matching cloud-init NIC configuration and Ansible inventory. Run `mise exec
   -- uv run python scripts/lab.py check`.
2. Replace the example `roles/demo_firewall` with roles for your VPS. The
   Molecule converge playbook applies them to the `targets` inventory group.
   Keep client and router configuration in the scenario or move it into roles.
3. Edit [expectations.yml](expectations.yml) to state which source VM, target
   VM, segment, protocol, and port should succeed. Tests run every entry over
   each configured IP family. Keep this file independent of your firewall
   role's input variables so a mistaken rule can fail the tests.
4. Extend the probe tests for your application protocol after the reachability
   tests pass. The supplied echo service deliberately listens on allowed and
   blocked ports, so a negative result tests the firewall rather than a closed
   service.

The starter policy allows 8080/TCP and 8081/UDP from the external client and
9090/TCP and 9091/UDP from the internal client. It blocks the opposite ports,
and the router blocks direct client-to-client forwarding. Probe failures
include the guest's route and selected interface.

The [production inventory example](inventories/production/hosts.yml.example)
and [playbook example](playbooks/site.yml.example) are placeholders for a real
VPS. Copy and edit them before deploying; no production host or credential is
included in the template.

## CI and alternatives

GitHub Actions runs the entire Molecule suite on a hosted Ubuntu runner. The
workflow grants the runner access to `/dev/kvm` when that device is present.
The lab uses KVM when accessible and falls back to QEMU software emulation if
KVM is absent or fails during startup. Software emulation can be slow.

If Ansible and Molecule add more machinery than you need, keep
`scripts/lab.py`, `topology.yml`, and the pytest probes. Run `lab.py up`,
configure the guests with cloud-init or shell scripts, run pytest, then run
`lab.py down`. The same VM network and test logic still apply. See [the
alternative workflow](docs/without-ansible.md) for details.

This project uses [Alpine cloud images](https://www.alpinelinux.org/cloud/),
[QEMU socket
networking](https://www.qemu.org/docs/master/system/devices/net.html), and
[vswitch](https://github.com/kruton/vswitch).
