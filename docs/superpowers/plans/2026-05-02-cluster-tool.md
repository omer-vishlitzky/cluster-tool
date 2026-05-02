# cluster-tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI tool that clones SNO clusters from a snapshot in ~2 minutes with full identity regeneration via recert.

**Architecture:** Single Python script runs locally, SSHes into baremetal to manage libvirt VMs/networks, runs recert inside each clone VM for identity regeneration, configures HAProxy and /etc/hosts for access.

**Tech Stack:** Python 3 stdlib only (subprocess, json, argparse, textwrap). Remote: libvirt/virsh, qemu-img, podman, recert.

**Spec:** `docs/superpowers/specs/2026-05-02-cluster-tool-design.md`

---

## File Structure

- **Create:** `cluster-tool` — the CLI tool (single executable Python script)
- **Create:** `test_cluster_tool.py` — unit tests for pure functions (templates, state)

---

### Task 1: CLI Skeleton, Constants, and State Management

**Files:**
- Create: `cluster-tool`
- Create: `test_cluster_tool.py`

- [ ] **Step 1: Write tests for state management and ID generation**

```python
# test_cluster_tool.py
import importlib.util
import json
import os
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("cluster_tool", "./cluster-tool")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "state.json")
        ct.LOCAL_STATE_FILE = type(ct.LOCAL_STATE_FILE)(self.state_file)
        ct.LOCAL_STATE_DIR = type(ct.LOCAL_STATE_DIR)(self.tmpdir)

    def test_load_empty_state(self):
        state = ct.load_state()
        self.assertIsNone(state["snapshot"])
        self.assertEqual(state["clones"], {})
        self.assertEqual(state["next_subnet"], 160)

    def test_save_and_load_roundtrip(self):
        state = {"snapshot": {"source": "abc"}, "clones": {}, "next_subnet": 162}
        ct.save_state(state)
        loaded = ct.load_state()
        self.assertEqual(loaded, state)

    def test_allocate_subnet_increments(self):
        state = ct.load_state()
        s1 = ct.allocate_subnet(state)
        s2 = ct.allocate_subnet(state)
        self.assertEqual(s1, 160)
        self.assertEqual(s2, 161)
        self.assertEqual(state["next_subnet"], 162)


class TestIDGeneration(unittest.TestCase):
    def test_clone_id_format(self):
        cid = ct.generate_clone_id()
        self.assertEqual(len(cid), 8)
        int(cid, 16)  # must be valid hex

    def test_clone_ids_are_unique(self):
        ids = {ct.generate_clone_id() for _ in range(100)}
        self.assertGreater(len(ids), 90)

    def test_mac_format(self):
        mac = ct.generate_mac()
        parts = mac.split(":")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "02")
        self.assertEqual(parts[1], "00")
        self.assertEqual(parts[2], "00")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 test_cluster_tool.py -v`
Expected: ImportError or AttributeError (cluster-tool doesn't exist yet)

- [ ] **Step 3: Create cluster-tool with skeleton, constants, and state management**

```python
#!/usr/bin/env python3
import argparse
import base64
import json
import random
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

BAREMETAL_HOST = "root@rdu-infra-edge-07.infra-edge.lab.eng.rdu2.redhat.com"
BAREMETAL_IP = "10.1.155.16"
SOURCE_CLUSTER_ID = "6ef80144"
SOURCE_DISK = "/data/test/assisted-test-infra/storage_pool/test-infra-cluster-6ef80144/test-infra-cluster-6ef80144-master-0-disk-0"
SOURCE_PRIMARY_SUBNET = 135
SOURCE_SECONDARY_SUBNET = 153
REMOTE_BASE = "/root/.cluster-tool"
REMOTE_SNAPSHOT = f"{REMOTE_BASE}/golden-snapshot.qcow2"
REMOTE_OVERLAYS = f"{REMOTE_BASE}/overlays"
LOCAL_STATE_DIR = Path.home() / ".cluster-tool"
LOCAL_STATE_FILE = LOCAL_STATE_DIR / "state.json"
KUBECONFIG_DIR = Path.home() / ".kube"
SUBNET_START = 160
SUBNET_SECONDARY_OFFSET = 18
VM_IP_SUFFIX = 10
VM_MEMORY_KIB = 67108864
VM_VCPUS = 16
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
RECERT_IMAGE = "quay.io/edge-infrastructure/recert:latest"
ETCD_IMAGE = "quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:96e7c7f2ccac380f0b57c6b3be73c09df05d46b3ff7864acb1ba4fc063d72508"
HOSTS_ENTRIES = [
    "api.{domain}",
    "apps.{domain}",
    "console-openshift-console.apps.{domain}",
    "oauth-openshift.apps.{domain}",
    "downloads-openshift-console.apps.{domain}",
    "alertmanager-main-openshift-monitoring.apps.{domain}",
    "prometheus-k8s-openshift-monitoring.apps.{domain}",
    "prometheus-k8s-federate-openshift-monitoring.apps.{domain}",
    "thanos-querier-openshift-monitoring.apps.{domain}",
]


def load_state():
    if LOCAL_STATE_FILE.exists():
        return json.loads(LOCAL_STATE_FILE.read_text())
    return {"snapshot": None, "clones": {}, "next_subnet": SUBNET_START}


def save_state(state):
    LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def generate_clone_id():
    return f"{random.randint(0, 0xFFFFFFFF):08x}"


def allocate_subnet(state):
    subnet = state["next_subnet"]
    state["next_subnet"] = subnet + 1
    return subnet


def generate_mac():
    return "02:00:00:{:02x}:{:02x}:{:02x}".format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )


def main():
    parser = argparse.ArgumentParser(description="Snapshot-based SNO cluster cloning")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("snapshot", help="Create golden snapshot from running cluster")

    boot_p = sub.add_parser("boot", help="Boot a fresh cluster from snapshot")
    boot_p.add_argument("--name", help="Clone ID (8 hex chars, random if omitted)")

    sub.add_parser("list", help="Show running clones")

    destroy_p = sub.add_parser("destroy", help="Tear down a clone")
    destroy_p.add_argument("name", nargs="?", help="Clone ID to destroy")
    destroy_p.add_argument("--all", action="store_true", help="Destroy all clones")

    args = parser.parse_args()
    cmds = {"snapshot": cmd_snapshot, "boot": cmd_boot, "list": cmd_list, "destroy": cmd_destroy}

    if args.command not in cmds:
        parser.print_help()
        sys.exit(1)
    cmds[args.command](args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `chmod +x cluster-tool && python3 test_cluster_tool.py -v`
Expected: All 6 tests pass

- [ ] **Step 5: Commit**

```bash
git add cluster-tool test_cluster_tool.py
git commit -m "cluster-tool: CLI skeleton with state management and ID generation"
```

---

### Task 2: Template Generators

**Files:**
- Modify: `cluster-tool`
- Modify: `test_cluster_tool.py`

- [ ] **Step 1: Add template tests to test file**

Append to `test_cluster_tool.py` before `if __name__`:

```python
class TestTemplates(unittest.TestCase):
    def test_primary_network_xml(self):
        xml = ct.gen_primary_network_xml("a1b2c3d4", 160, "02:00:00:aa:bb:cc")
        self.assertIn("<name>test-infra-net-a1b2c3d4</name>", xml)
        self.assertIn("192.168.160.1", xml)
        self.assertIn("192.168.160.10", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("api.test-infra-cluster-a1b2c3d4.redhat.com", xml)
        self.assertIn("192.168.160.100", xml)
        self.assertIn("192.168.160.101", xml)

    def test_secondary_network_xml(self):
        xml = ct.gen_secondary_network_xml("a1b2c3d4", 160, 178, "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-secondary-network-a1b2c3d4</name>", xml)
        self.assertIn("192.168.178.1", xml)
        self.assertIn("192.168.178.10", xml)
        self.assertIn("192.168.160.100", xml)  # DNS points to primary VIP

    def test_vm_xml(self):
        xml = ct.gen_vm_xml("a1b2c3d4", "/path/overlay.qcow2", "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-cluster-a1b2c3d4-master-0</name>", xml)
        self.assertIn("/path/overlay.qcow2", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("02:00:00:dd:ee:ff", xml)
        self.assertIn("test-infra-net-a1b2c3d4", xml)
        self.assertIn("test-infra-secondary-network-a1b2c3d4", xml)
        self.assertIn(str(ct.VM_MEMORY_KIB), xml)

    def test_haproxy_additions(self):
        use_backends, backends = ct.gen_haproxy_additions("a1b2c3d4", 160)
        self.assertIn("api-a1b2c3d4", use_backends["api"])
        self.assertIn("req_ssl_sni", use_backends["api"])
        self.assertIn("hdr_end(host)", use_backends["ingress-http"])
        self.assertIn("192.168.160.10:6443", backends)
        self.assertIn("192.168.160.10:443", backends)
        self.assertIn("192.168.160.10:80", backends)

    def test_hosts_block(self):
        block = ct.gen_hosts_block("a1b2c3d4")
        self.assertIn("cluster-tool:begin:a1b2c3d4", block)
        self.assertIn("cluster-tool:end:a1b2c3d4", block)
        self.assertIn("10.1.155.16 api.test-infra-cluster-a1b2c3d4.redhat.com", block)
        self.assertIn("console-openshift-console.apps.test-infra-cluster-a1b2c3d4.redhat.com", block)
        self.assertEqual(block.count("10.1.155.16"), len(ct.HOSTS_ENTRIES))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 test_cluster_tool.py -v`
Expected: AttributeError for missing gen_* functions

- [ ] **Step 3: Add template generators to cluster-tool**

Add after `generate_mac()` and before `main()`:

```python
def gen_primary_network_xml(clone_id, subnet, mac):
    domain = f"test-infra-cluster-{clone_id}.redhat.com"
    return textwrap.dedent(f"""\
        <network>
          <name>test-infra-net-{clone_id}</name>
          <forward mode='nat'><nat><port start='1024' end='65535'/></nat></forward>
          <bridge stp='on' delay='0'/>
          <mtu size='1500'/>
          <domain name='{domain}' localOnly='yes'/>
          <dns enable='yes'>
            <host ip='192.168.{subnet}.100'>
              <hostname>api-int.{domain}</hostname>
              <hostname>api.{domain}</hostname>
            </host>
            <host ip='192.168.{subnet}.101'>
              <hostname>oauth-openshift.apps.{domain}</hostname>
              <hostname>console-openshift-console.apps.{domain}</hostname>
              <hostname>canary-openshift-ingress-canary.apps.{domain}</hostname>
            </host>
          </dns>
          <ip family='ipv4' address='192.168.{subnet}.1' prefix='24'>
            <dhcp>
              <range start='192.168.{subnet}.128' end='192.168.{subnet}.254'/>
              <host mac='{mac}' name='test-infra-cluster-{clone_id}-master-0' ip='192.168.{subnet}.{VM_IP_SUFFIX}'/>
            </dhcp>
          </ip>
        </network>""")


def gen_secondary_network_xml(clone_id, subnet_primary, subnet_secondary, mac):
    domain = f"test-infra-cluster-{clone_id}.redhat.com"
    return textwrap.dedent(f"""\
        <network>
          <name>test-infra-secondary-network-{clone_id}</name>
          <forward mode='nat'><nat><port start='1024' end='65535'/></nat></forward>
          <bridge stp='on' delay='0'/>
          <mtu size='1500'/>
          <domain name='{domain}' localOnly='yes'/>
          <dns enable='yes'>
            <host ip='192.168.{subnet_primary}.100'>
              <hostname>api-int.{domain}</hostname>
              <hostname>api.{domain}</hostname>
            </host>
            <host ip='192.168.{subnet_primary}.101'>
              <hostname>oauth-openshift.apps.{domain}</hostname>
              <hostname>console-openshift-console.apps.{domain}</hostname>
              <hostname>canary-openshift-ingress-canary.apps.{domain}</hostname>
            </host>
          </dns>
          <ip family='ipv4' address='192.168.{subnet_secondary}.1' prefix='24'>
            <dhcp>
              <range start='192.168.{subnet_secondary}.128' end='192.168.{subnet_secondary}.254'/>
              <host mac='{mac}' name='test-infra-cluster-{clone_id}-master-0' ip='192.168.{subnet_secondary}.{VM_IP_SUFFIX}'/>
            </dhcp>
          </ip>
        </network>""")


def gen_vm_xml(clone_id, overlay_disk, mac_primary, mac_secondary):
    return textwrap.dedent(f"""\
        <domain type='kvm'>
          <name>test-infra-cluster-{clone_id}-master-0</name>
          <memory unit='KiB'>{VM_MEMORY_KIB}</memory>
          <currentMemory unit='KiB'>{VM_MEMORY_KIB}</currentMemory>
          <vcpu placement='static'>{VM_VCPUS}</vcpu>
          <os>
            <type arch='x86_64' machine='pc-i440fx-rhel7.6.0'>hvm</type>
            <boot dev='hd'/>
          </os>
          <features><acpi/><apic/><pae/></features>
          <cpu mode='host-passthrough' check='none' migratable='on'/>
          <clock offset='utc'/>
          <on_poweroff>destroy</on_poweroff>
          <on_reboot>restart</on_reboot>
          <on_crash>destroy</on_crash>
          <devices>
            <emulator>/usr/libexec/qemu-kvm</emulator>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{overlay_disk}'/>
              <target dev='sda' bus='scsi'/>
            </disk>
            <controller type='scsi' index='0' model='virtio-scsi'/>
            <interface type='network'>
              <mac address='{mac_primary}'/>
              <source network='test-infra-net-{clone_id}'/>
              <model type='virtio'/>
            </interface>
            <interface type='network'>
              <mac address='{mac_secondary}'/>
              <source network='test-infra-secondary-network-{clone_id}'/>
              <model type='virtio'/>
            </interface>
            <serial type='pty'><target port='0'/></serial>
            <console type='pty'><target type='serial' port='0'/></console>
          </devices>
        </domain>""")


def gen_haproxy_additions(clone_id, subnet):
    ip = f"192.168.{subnet}.{VM_IP_SUFFIX}"
    domain = f"test-infra-cluster-{clone_id}.redhat.com"
    use_backends = {
        "api": f"    use_backend api-{clone_id} if {{ req_ssl_sni -m end {domain} }}",
        "ingress-https": f"    use_backend ingress-https-{clone_id} if {{ req_ssl_sni -m end {domain} }}",
        "ingress-http": f"    use_backend ingress-http-{clone_id} if {{ hdr_end(host) {domain} }}",
    }
    backends = (
        f"\nbackend api-{clone_id}\n"
        f"    server api {ip}:6443 check\n"
        f"\nbackend ingress-https-{clone_id}\n"
        f"    server ingress {ip}:443 check\n"
        f"\nbackend ingress-http-{clone_id}\n"
        f"    mode http\n"
        f"    server ingress {ip}:80 check\n"
    )
    return use_backends, backends


def gen_hosts_block(clone_id):
    domain = f"test-infra-cluster-{clone_id}.redhat.com"
    lines = [f"# cluster-tool:begin:{clone_id}"]
    for entry in HOSTS_ENTRIES:
        lines.append(f"{BAREMETAL_IP} {entry.format(domain=domain)}")
    lines.append(f"# cluster-tool:end:{clone_id}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 test_cluster_tool.py -v`
Expected: All 11 tests pass

- [ ] **Step 5: Commit**

```bash
git add cluster-tool test_cluster_tool.py
git commit -m "cluster-tool: add template generators for network XML, VM XML, HAProxy, and /etc/hosts"
```

---

### Task 3: SSH Helpers, Progress Output, and Infrastructure Management

**Files:**
- Modify: `cluster-tool`

- [ ] **Step 1: Add SSH helpers, progress output, and HAProxy/hosts management**

Add after the template generators, before `main()`:

```python
def print_step(num, total, desc, status=""):
    if status:
        print(f"\r  [{num}/{total}] {desc:<40s} {status}")
    else:
        print(f"  [{num}/{total}] {desc:<40s}", end="", flush=True)


def ssh_baremetal(cmd, check=True):
    r = subprocess.run(
        ["ssh"] + SSH_OPTS + [BAREMETAL_HOST, cmd],
        capture_output=True, text=True, check=check,
    )
    return r


def ssh_vm(vm_ip, cmd, check=True):
    jump = f"ssh {' '.join(SSH_OPTS)} core@{vm_ip} '{cmd}'"
    return ssh_baremetal(jump, check=check)


def scp_from_baremetal(remote_path, local_path):
    subprocess.run(
        ["scp"] + SSH_OPTS + [f"{BAREMETAL_HOST}:{remote_path}", str(local_path)],
        capture_output=True, text=True, check=True,
    )


def write_remote_file(path, content):
    encoded = base64.b64encode(content.encode()).decode()
    ssh_baremetal(f"echo '{encoded}' | base64 -d > {path}")


def add_haproxy_clone(clone_id, subnet):
    use_backends, backends = gen_haproxy_additions(clone_id, subnet)
    result = ssh_baremetal("cat /etc/haproxy/haproxy.cfg")
    config = result.stdout

    for key in ["api", "ingress-https", "ingress-http"]:
        marker = f"    default_backend {key}-" if key != "api" else "    default_backend api-"
        config = config.replace(marker, f"{use_backends[key]}\n\n{marker}", 1)

    config = config.rstrip() + "\n" + backends
    write_remote_file("/etc/haproxy/haproxy.cfg", config)
    ssh_baremetal("systemctl reload haproxy")


def remove_haproxy_clone(clone_id):
    result = ssh_baremetal("cat /etc/haproxy/haproxy.cfg")
    lines = result.stdout.split("\n")
    filtered = []
    skip_block = False
    for line in lines:
        if f"use_backend" in line and f"-{clone_id}" in line:
            continue
        if line.strip().startswith("backend") and f"-{clone_id}" in line:
            skip_block = True
            continue
        if skip_block:
            if line.strip() and not line.strip().startswith("backend") and not line.strip().startswith("frontend"):
                continue
            skip_block = False
            if not line.strip():
                continue
        filtered.append(line)
    write_remote_file("/etc/haproxy/haproxy.cfg", "\n".join(filtered))
    ssh_baremetal("systemctl reload haproxy")


def add_hosts_entries(clone_id):
    block = gen_hosts_block(clone_id)
    subprocess.run(
        ["sudo", "tee", "-a", "/etc/hosts"],
        input="\n" + block + "\n", capture_output=True, text=True, check=True,
    )


def remove_hosts_entries(clone_id):
    subprocess.run(
        ["sudo", "sed", "-i",
         f"/cluster-tool:begin:{clone_id}/,/cluster-tool:end:{clone_id}/d",
         "/etc/hosts"],
        capture_output=True, text=True, check=True,
    )
```

- [ ] **Step 2: Manually verify SSH connectivity**

Run: `python3 -c "import importlib.util; s=importlib.util.spec_from_file_location('ct','./cluster-tool'); ct=importlib.util.module_from_spec(s); s.loader.exec_module(ct); r=ct.ssh_baremetal('hostname'); print(r.stdout)"`
Expected: prints `rdu-infra-edge-07.infra-edge.lab.eng.rdu2.redhat.com`

- [ ] **Step 3: Commit**

```bash
git add cluster-tool
git commit -m "cluster-tool: add SSH helpers, progress output, HAProxy and hosts management"
```

---

### Task 4: Snapshot Command

**Files:**
- Modify: `cluster-tool`

- [ ] **Step 1: Implement cmd_snapshot**

Add before `main()`:

```python
def cmd_snapshot(args):
    state = load_state()
    vm_name = f"test-infra-cluster-{SOURCE_CLUSTER_ID}-master-0"
    print(f"Creating golden snapshot from {vm_name}...")

    print_step(1, 4, "Shutting down VM")
    ssh_baremetal(f"virsh shutdown {vm_name}")
    for _ in range(60):
        r = ssh_baremetal(f"virsh domstate {vm_name}")
        if "shut off" in r.stdout:
            break
        time.sleep(2)
    else:
        sys.exit("VM did not shut down in 2 minutes")
    print_step(1, 4, "Shutting down VM", "done")

    print_step(2, 4, "Creating directories")
    ssh_baremetal(f"mkdir -p {REMOTE_BASE} {REMOTE_OVERLAYS}")
    print_step(2, 4, "Creating directories", "done")

    print_step(3, 4, "Copying disk (~33GB, this takes a few minutes)")
    ssh_baremetal(f"cp --sparse=always {SOURCE_DISK} {REMOTE_SNAPSHOT}")
    print_step(3, 4, "Copying disk (~33GB)", "done")

    print_step(4, 4, "Restarting source VM")
    ssh_baremetal(f"virsh start {vm_name}")
    print_step(4, 4, "Restarting source VM", "done")

    state["snapshot"] = {
        "source_cluster": SOURCE_CLUSTER_ID,
        "source_disk": SOURCE_DISK,
        "golden_snapshot": REMOTE_SNAPSHOT,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    print(f"\nSnapshot created at {REMOTE_SNAPSHOT}")
```

- [ ] **Step 2: Test snapshot command against real cluster**

Run: `./cluster-tool snapshot`
Expected: VM shuts down, disk copies (~2-3 min), VM restarts. State file created at `~/.cluster-tool/state.json`.

Verify: `cat ~/.cluster-tool/state.json` shows snapshot metadata.
Verify: `ssh root@rdu-infra-edge-07... "ls -la /root/.cluster-tool/golden-snapshot.qcow2"` shows the file.

- [ ] **Step 3: Commit**

```bash
git add cluster-tool
git commit -m "cluster-tool: implement snapshot command"
```

---

### Task 5: Boot Command

**Files:**
- Modify: `cluster-tool`

This is the main command. It creates a clone VM, runs recert, and configures access.

- [ ] **Step 1: Implement cmd_boot**

Add before `main()`:

```python
def cmd_boot(args):
    state = load_state()
    if not state.get("snapshot"):
        sys.exit("No snapshot found. Run 'cluster-tool snapshot' first.")

    clone_id = args.name or generate_clone_id()
    if clone_id in state.get("clones", {}):
        sys.exit(f"Clone '{clone_id}' already exists.")

    subnet = allocate_subnet(state)
    subnet_sec = subnet + SUBNET_SECONDARY_OFFSET
    mac1, mac2 = generate_mac(), generate_mac()
    vm_ip = f"192.168.{subnet}.{VM_IP_SUFFIX}"
    overlay = f"{REMOTE_OVERLAYS}/{clone_id}.qcow2"
    vm_name = f"test-infra-cluster-{clone_id}-master-0"
    domain = f"test-infra-cluster-{clone_id}.redhat.com"

    state["clones"][clone_id] = {
        "subnet_primary": subnet,
        "subnet_secondary": subnet_sec,
        "vm_name": vm_name,
        "overlay_disk": overlay,
        "mac_primary": mac1,
        "mac_secondary": mac2,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)

    total = 7
    print(f'Creating clone "{clone_id}"...')

    # 1 - disk overlay
    print_step(1, total, "Creating disk overlay")
    ssh_baremetal(f"qemu-img create -f qcow2 -b {REMOTE_SNAPSHOT} -F qcow2 {overlay}")
    print_step(1, total, "Creating disk overlay", "done")

    # 2 - networks
    print_step(2, total, f"Creating network 192.168.{subnet}.0/24")
    primary_xml = gen_primary_network_xml(clone_id, subnet, mac1)
    secondary_xml = gen_secondary_network_xml(clone_id, subnet, subnet_sec, mac2)
    write_remote_file(f"/tmp/net-{clone_id}.xml", primary_xml)
    ssh_baremetal(f"virsh net-define /tmp/net-{clone_id}.xml && virsh net-start test-infra-net-{clone_id}")
    write_remote_file(f"/tmp/snet-{clone_id}.xml", secondary_xml)
    ssh_baremetal(f"virsh net-define /tmp/snet-{clone_id}.xml && virsh net-start test-infra-secondary-network-{clone_id}")
    ssh_baremetal(f"rm -f /tmp/net-{clone_id}.xml /tmp/snet-{clone_id}.xml")
    print_step(2, total, f"Creating network 192.168.{subnet}.0/24", "done")

    # 3 - boot VM
    print_step(3, total, "Booting VM")
    vm_xml = gen_vm_xml(clone_id, overlay, mac1, mac2)
    write_remote_file(f"/tmp/vm-{clone_id}.xml", vm_xml)
    ssh_baremetal(f"virsh define /tmp/vm-{clone_id}.xml && virsh start {vm_name}")
    ssh_baremetal(f"rm -f /tmp/vm-{clone_id}.xml")
    print_step(3, total, "Booting VM", "done")

    # 4 - wait for SSH
    print_step(4, total, "Waiting for SSH")
    for _ in range(60):
        if ssh_vm(vm_ip, "echo ready", check=False).returncode == 0:
            break
        time.sleep(5)
    else:
        sys.exit("SSH not available after 5 minutes")
    print_step(4, total, "Waiting for SSH", "done")

    # 5 - recert
    print_step(5, total, "Running recert")
    src_ip = f"192.168.{SOURCE_PRIMARY_SUBNET}.{VM_IP_SUFFIX}"
    src_sec_ip = f"192.168.{SOURCE_SECONDARY_SUBNET}.{VM_IP_SUFFIX}"
    new_sec_ip = f"192.168.{subnet_sec}.{VM_IP_SUFFIX}"

    ssh_vm(vm_ip, "sudo systemctl stop kubelet")
    time.sleep(5)
    ssh_vm(vm_ip, "sudo systemctl stop crio")
    time.sleep(5)

    ssh_vm(vm_ip,
        f"sudo podman run -d --name etcd-recert "
        f"--network host --privileged "
        f"-v /var/lib/etcd:/var/lib/etcd:Z "
        f"{ETCD_IMAGE} etcd "
        f"--data-dir /var/lib/etcd "
        f"--listen-client-urls http://localhost:2379 "
        f"--advertise-client-urls http://localhost:2379 "
        f"--listen-peer-urls http://localhost:2380 "
        f"--force-new-cluster")
    time.sleep(10)

    ssh_vm(vm_ip,
        f"sudo podman run --rm --name recert "
        f"--network host --privileged "
        f"-v /etc/kubernetes:/kubernetes "
        f"-v /var/lib/kubelet:/kubelet "
        f"-v /etc/machine-config-daemon:/machine-config-daemon "
        f"-v /etc:/host-etc "
        f"{RECERT_IMAGE} "
        f"--etcd-endpoint localhost:2379 "
        f"--cluster-rename test-infra-cluster-{clone_id}:{domain} "
        f"--hostname {vm_name} "
        f"--ip {vm_ip} "
        f"--cn-san-replace test-infra-cluster-{SOURCE_CLUSTER_ID}:test-infra-cluster-{clone_id} "
        f"--cn-san-replace {src_ip}:{vm_ip} "
        f"--cn-san-replace {src_sec_ip}:{new_sec_ip}")

    ssh_vm(vm_ip, "sudo podman stop etcd-recert; sudo podman rm etcd-recert", check=False)
    ssh_vm(vm_ip, "sudo systemctl start crio")
    time.sleep(3)
    ssh_vm(vm_ip, "sudo systemctl start kubelet")
    print_step(5, total, "Running recert", "done")

    # 6 - wait for health
    print_step(6, total, "Waiting for cluster health")
    for _ in range(120):
        r = ssh_baremetal(f"curl -sk https://{vm_ip}:6443/healthz", check=False)
        if r.stdout.strip() == "ok":
            break
        time.sleep(5)
    else:
        sys.exit("Cluster not healthy after 10 minutes")
    print_step(6, total, "Waiting for cluster health", "done")

    # 7 - configure access
    print_step(7, total, "Configuring access")
    kubeconfig_path = KUBECONFIG_DIR / f"{clone_id}.kubeconfig"
    KUBECONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = f"/tmp/kubeconfig-{clone_id}"
    ssh_baremetal(
        f"scp {' '.join(SSH_OPTS)} core@{vm_ip}:"
        f"/etc/kubernetes/static-pod-resources/kube-apiserver-certs/secrets/node-kubeconfigs/lb-ext.kubeconfig {tmp}"
    )
    scp_from_baremetal(tmp, kubeconfig_path)
    ssh_baremetal(f"rm -f {tmp}")

    kc = kubeconfig_path.read_text()
    kc = kc.replace(f"https://{vm_ip}:6443", f"https://api.{domain}:6443")
    kubeconfig_path.write_text(kc)

    add_haproxy_clone(clone_id, subnet)
    add_hosts_entries(clone_id)
    print_step(7, total, "Configuring access", "done")

    print(f'\nCluster "{clone_id}" ready!')
    print(f"  API:        https://api.{domain}:6443")
    print(f"  Console:    https://console-openshift-console.apps.{domain}")
    print(f"  Kubeconfig: {kubeconfig_path}")
    print(f"\nexport KUBECONFIG={kubeconfig_path}")
```

- [ ] **Step 2: Test boot command against real cluster**

Run: `sudo ./cluster-tool boot --name testboot1`
Expected output (over ~2 minutes):
```
Creating clone "testboot1"...
  [1/7] Creating disk overlay                    done
  [2/7] Creating network 192.168.160.0/24        done
  [3/7] Booting VM                               done
  [4/7] Waiting for SSH                          done
  [5/7] Running recert                           done
  [6/7] Waiting for cluster health               done
  [7/7] Configuring access                       done

Cluster "testboot1" ready!
  API:        https://api.test-infra-cluster-testboot1.redhat.com:6443
  ...
```

Verify cluster health:
```bash
export KUBECONFIG=~/.kube/testboot1.kubeconfig
oc get nodes
oc get clusterversion
oc get co
```

- [ ] **Step 3: Commit**

```bash
git add cluster-tool
git commit -m "cluster-tool: implement boot command with recert identity regeneration"
```

---

### Task 6: List and Destroy Commands

**Files:**
- Modify: `cluster-tool`

- [ ] **Step 1: Implement cmd_list and cmd_destroy**

Add before `main()`:

```python
def cmd_list(args):
    state = load_state()
    clones = state.get("clones", {})
    if not clones:
        print("No clones running.")
        return
    print(f"{'NAME':<14s} {'SUBNET':<22s} {'VM':<50s} {'CREATED'}")
    print("-" * 110)
    for cid, info in clones.items():
        subnet = f"192.168.{info['subnet_primary']}.0/24"
        created = info.get("created_at", "?")[:19]
        print(f"{cid:<14s} {subnet:<22s} {info['vm_name']:<50s} {created}")


def cmd_destroy(args):
    state = load_state()
    if args.all:
        ids = list(state.get("clones", {}).keys())
        if not ids:
            print("No clones to destroy.")
            return
    elif args.name:
        ids = [args.name]
    else:
        sys.exit("Specify a clone name or --all")

    for cid in ids:
        if cid not in state.get("clones", {}):
            print(f"Clone '{cid}' not found, skipping.")
            continue
        info = state["clones"][cid]
        vm = info["vm_name"]
        print(f'Destroying clone "{cid}"...')

        ssh_baremetal(f"virsh destroy {vm}", check=False)
        ssh_baremetal(f"virsh undefine {vm}", check=False)
        ssh_baremetal(f"virsh net-destroy test-infra-net-{cid}", check=False)
        ssh_baremetal(f"virsh net-undefine test-infra-net-{cid}", check=False)
        ssh_baremetal(f"virsh net-destroy test-infra-secondary-network-{cid}", check=False)
        ssh_baremetal(f"virsh net-undefine test-infra-secondary-network-{cid}", check=False)
        ssh_baremetal(f"rm -f {info['overlay_disk']}", check=False)

        remove_haproxy_clone(cid)
        remove_hosts_entries(cid)
        (KUBECONFIG_DIR / f"{cid}.kubeconfig").unlink(missing_ok=True)
        del state["clones"][cid]
        print(f'Clone "{cid}" destroyed.')

    save_state(state)
```

- [ ] **Step 2: Test list command**

Run: `./cluster-tool list`
Expected: Shows the clone created in Task 5 with its subnet, VM name, and creation time.

- [ ] **Step 3: Test destroy command**

Run: `sudo ./cluster-tool destroy testboot1`
Expected: VM destroyed, networks removed, HAProxy updated, /etc/hosts cleaned, kubeconfig deleted.

Verify cleanup:
```bash
ssh root@rdu-infra-edge-07... "virsh list --all | grep testboot1"  # should be empty
ssh root@rdu-infra-edge-07... "virsh net-list --all | grep testboot1"  # should be empty
grep testboot1 /etc/hosts  # should be empty
ls ~/.kube/testboot1.kubeconfig  # should not exist
```

- [ ] **Step 4: Commit**

```bash
git add cluster-tool
git commit -m "cluster-tool: implement list and destroy commands"
```

---

### Task 7: End-to-End Verification

- [ ] **Step 1: Full cycle test**

```bash
# Create snapshot (skip if already done)
./cluster-tool snapshot

# Boot first clone
sudo ./cluster-tool boot --name e2e01

# Verify cluster
export KUBECONFIG=~/.kube/e2e01.kubeconfig
oc get nodes -o wide
oc get clusterversion
oc get co | grep -v 'True.*False.*False' || echo "All operators healthy"

# List clones
./cluster-tool list
```

Expected: Cluster is healthy, all operators converge, node shows new hostname/IP.

- [ ] **Step 2: Parallel clone test**

```bash
# Boot a second clone while first is running
sudo ./cluster-tool boot --name e2e02

# Verify both clusters independently
KUBECONFIG=~/.kube/e2e01.kubeconfig oc get nodes
KUBECONFIG=~/.kube/e2e02.kubeconfig oc get nodes

# List should show both
./cluster-tool list
```

Expected: Both clusters run independently with different names, IPs, and certificates.

- [ ] **Step 3: Destroy all and verify clean state**

```bash
sudo ./cluster-tool destroy --all
./cluster-tool list  # should show "No clones running."
grep cluster-tool /etc/hosts  # should be empty
cat ~/.cluster-tool/state.json  # clones should be empty
```

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "cluster-tool: verified end-to-end with parallel clones"
```
