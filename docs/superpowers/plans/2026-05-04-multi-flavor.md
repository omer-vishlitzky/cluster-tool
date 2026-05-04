# Multi-Flavor Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support multiple named snapshot flavors so different VM configurations (64GB SNO, 32GB SNO, SNO+CNV, etc.) can each be snapshotted and booted independently.

**Architecture:** Replace hardcoded source cluster constants with per-flavor metadata auto-detected from the source VM via `virsh dominfo`/`virsh dumpxml`. Store each flavor in its own directory under `/data/cluster-tool/flavors/<name>/`. State file changes from `"snapshot"` (singular) to `"flavors"` (dict).

**Tech Stack:** Python 3 stdlib only. Remote: virsh, qemu-img (same as current).

---

## File Structure

- **Modify:** `cluster-tool` — all changes in this single file
- **Modify:** `test_cluster_tool.py` — update tests for new signatures and add flavor tests

---

### Task 1: Auto-detect source VM specs

Add a function that SSHes into the baremetal, runs `virsh dominfo` and `virsh dumpxml` on the source VM, and returns a metadata dict with memory, vcpus, disk paths, primary/secondary subnet, and the etcd image.

**Files:**
- Modify: `cluster-tool`
- Modify: `test_cluster_tool.py`

- [ ] **Step 1: Write test for detect_source_vm**

Add to `test_cluster_tool.py` before `if __name__`:

```python
class TestDetectSourceVM(unittest.TestCase):
    def test_parse_dominfo(self):
        dominfo = """Id:             90
Name:           test-infra-cluster-6ef80144-master-0
UUID:           bd3a9dd8-cc6b-4aae-8ba0-ca4b1c6b238d
OS Type:        hvm
State:          running
CPU(s):         16
Max memory:     67108864 KiB
Used memory:    67108864 KiB"""
        result = ct.parse_dominfo(dominfo)
        self.assertEqual(result["vcpus"], 16)
        self.assertEqual(result["memory_kib"], 67108864)

    def test_parse_disk_paths(self):
        dumpxml = """<domain type='kvm'>
          <devices>
            <disk type='volume' device='disk'>
              <source pool='test-pool' volume='disk-0'/>
              <target dev='sda' bus='scsi'/>
            </disk>
            <disk type='file' device='cdrom'>
              <source file='/tmp/installer.iso'/>
              <target dev='vdz' bus='scsi'/>
              <readonly/>
            </disk>
            <disk type='file' device='disk'>
              <source file='/data/extra-disk.qcow2'/>
              <target dev='sdb' bus='scsi'/>
            </disk>
          </devices>
        </domain>"""
        disks = ct.parse_disk_paths(dumpxml, "test-pool")
        self.assertEqual(len(disks), 2)
        self.assertIn("sda", disks[0]["target"])
        self.assertIn("sdb", disks[1]["target"])
        # CDROMs should be excluded

    def test_parse_subnets(self):
        net_xml = """<network>
          <ip family='ipv4' address='192.168.135.1' prefix='24'>
            <dhcp>
              <host mac='02:00:00:02:4D:52' ip='192.168.135.10'/>
            </dhcp>
          </ip>
        </network>"""
        subnet = ct.parse_subnet(net_xml)
        self.assertEqual(subnet, 135)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 test_cluster_tool.py TestDetectSourceVM -v`
Expected: AttributeError (functions don't exist yet)

- [ ] **Step 3: Implement parse_dominfo, parse_disk_paths, parse_subnet**

Add after `generate_mac()` in `cluster-tool`:

```python
def parse_dominfo(dominfo_output):
    result = {}
    for line in dominfo_output.strip().split("\n"):
        if "CPU(s):" in line:
            result["vcpus"] = int(line.split(":")[1].strip())
        elif "Max memory:" in line:
            result["memory_kib"] = int(line.split(":")[1].strip().split()[0])
    return result


def parse_disk_paths(dumpxml_output, pool_name):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(dumpxml_output)
    disks = []
    for disk in root.findall(".//disk"):
        if disk.get("device") == "cdrom":
            continue
        target = disk.find("target")
        source = disk.find("source")
        if target is None or source is None:
            continue
        path = None
        if source.get("file"):
            path = source.get("file")
        elif source.get("pool") and source.get("volume"):
            r = ssh_baremetal(f"virsh vol-path --pool {source.get('pool')} {source.get('volume')}")
            path = r.stdout.strip()
        if path:
            disks.append({"target": target.get("dev"), "path": path})
    disks.sort(key=lambda d: d["target"])
    return disks


def parse_subnet(net_xml):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(net_xml)
    ip_elem = root.find(".//ip[@family='ipv4']")
    if ip_elem is None:
        ip_elem = root.find(".//ip")
    addr = ip_elem.get("address")
    return int(addr.split(".")[2])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 test_cluster_tool.py TestDetectSourceVM -v`
Expected: 3 tests pass. Note: `parse_disk_paths` with pool resolution calls `ssh_baremetal` — the test uses inline XML with `file` sources to avoid SSH.

- [ ] **Step 5: Commit**

```bash
git add cluster-tool test_cluster_tool.py
git commit -m "cluster-tool: add VM spec auto-detection (parse dominfo, disks, subnets)"
```

---

### Task 2: Refactor state and constants for multi-flavor

Replace the hardcoded `SOURCE_*` constants and single `"snapshot"` state with a per-flavor structure. Add `flavors` command.

**Files:**
- Modify: `cluster-tool`
- Modify: `test_cluster_tool.py`

- [ ] **Step 1: Write test for flavor state management**

Add to `test_cluster_tool.py`:

```python
class TestFlavorState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ct.LOCAL_STATE_FILE = Path(os.path.join(self.tmpdir, "state.json"))
        ct.LOCAL_STATE_DIR = Path(self.tmpdir)

    def test_load_empty_has_no_flavors(self):
        state = ct.load_state()
        self.assertEqual(state["flavors"], {})

    def test_save_flavor(self):
        state = ct.load_state()
        state["flavors"]["sno-64"] = {
            "source_cluster": "6ef80144",
            "memory_kib": 67108864,
            "vcpus": 16,
            "disks": ["disk-0.qcow2"],
            "source_primary_subnet": 135,
            "source_secondary_subnet": 153,
            "created_at": "2026-05-04T00:00:00Z",
        }
        ct.save_state(state)
        loaded = ct.load_state()
        self.assertIn("sno-64", loaded["flavors"])
        self.assertEqual(loaded["flavors"]["sno-64"]["vcpus"], 16)

    def test_backward_compat_old_state(self):
        old_state = {
            "snapshot": {"source_cluster": "6ef80144", "golden_snapshot": "/old/path"},
            "clones": {},
            "next_subnet": 165,
        }
        ct.save_state(old_state)
        state = ct.load_state()
        self.assertIn("flavors", state)
        self.assertEqual(state["next_subnet"], 165)
```

- [ ] **Step 2: Update load_state default and add migration**

In `cluster-tool`, update `load_state`:

```python
def load_state():
    if LOCAL_STATE_FILE.exists():
        state = json.loads(LOCAL_STATE_FILE.read_text())
        if "flavors" not in state:
            state["flavors"] = {}
            if state.get("snapshot"):
                state["flavors"]["default"] = state.pop("snapshot")
            else:
                state.pop("snapshot", None)
        return state
    return {"flavors": {}, "clones": {}, "next_subnet": SUBNET_START}
```

- [ ] **Step 3: Remove hardcoded SOURCE_* constants**

Remove these lines from the top of `cluster-tool`:
```python
SOURCE_CLUSTER_ID = "6ef80144"
SOURCE_DISK = "..."
SOURCE_PRIMARY_SUBNET = 135
SOURCE_SECONDARY_SUBNET = 153
VM_MEMORY_KIB = 67108864
VM_VCPUS = 16
REMOTE_SNAPSHOT = f"{REMOTE_BASE}/golden-snapshot.qcow2"
REMOTE_CRYPTO = f"{REMOTE_BASE}/crypto"
```

Replace with per-flavor path helpers:

```python
REMOTE_FLAVORS = f"{REMOTE_BASE}/flavors"

def flavor_dir(name):
    return f"{REMOTE_FLAVORS}/{name}"

def flavor_crypto_dir(name):
    return f"{REMOTE_FLAVORS}/{name}/crypto"
```

- [ ] **Step 4: Add `flavors` command**

```python
def cmd_flavors(args):
    state = load_state()
    if args.delete:
        name = args.delete
        if name in state["flavors"]:
            ssh_baremetal(f"rm -rf {flavor_dir(name)}", check=False)
            del state["flavors"][name]
            save_state(state)
            print(f'Flavor "{name}" deleted.')
        else:
            print(f'Flavor "{name}" not found.')
        return
    flavors = state.get("flavors", {})
    if not flavors:
        print("No flavors available. Run 'cluster-tool snapshot --name NAME --source CLUSTER_ID' to create one.")
        return
    print(f"{'NAME':<20s} {'SOURCE':<12s} {'RAM':<8s} {'CPU':<6s} {'DISKS':<6s} {'CREATED'}")
    print("-" * 80)
    for name, info in flavors.items():
        ram = f"{info.get('memory_kib', 0) // 1024 // 1024}GB"
        cpus = str(info.get("vcpus", "?"))
        disks = str(len(info.get("disks", [])))
        created = info.get("created_at", "?")[:19]
        source = info.get("source_cluster", "?")
        print(f"{name:<20s} {source:<12s} {ram:<8s} {cpus:<6s} {disks:<6s} {created}")
```

Update `main()` to add the new subcommands:

```python
def main():
    parser = argparse.ArgumentParser(description="Snapshot-based SNO cluster cloning")
    sub = parser.add_subparsers(dest="command")

    snap_p = sub.add_parser("snapshot", help="Create a named snapshot flavor from a running cluster")
    snap_p.add_argument("--name", required=True, help="Flavor name (e.g., sno-64, sno-cnv)")
    snap_p.add_argument("--source", required=True, help="Source cluster ID (the 8-hex-char part of the VM name)")

    boot_p = sub.add_parser("boot", help="Boot a fresh cluster from a snapshot flavor")
    boot_p.add_argument("--flavor", help="Flavor to boot from (default: most recent)")
    boot_p.add_argument("--name", help="Clone ID (random if omitted)")

    sub.add_parser("list", help="Show running clones")

    destroy_p = sub.add_parser("destroy", help="Tear down a clone")
    destroy_p.add_argument("name", nargs="?", help="Clone ID to destroy")
    destroy_p.add_argument("--all", action="store_true", help="Destroy all clones")

    flavors_p = sub.add_parser("flavors", help="List or manage snapshot flavors")
    flavors_p.add_argument("--delete", metavar="NAME", help="Delete a flavor")

    args = parser.parse_args()
    cmds = {
        "snapshot": cmd_snapshot, "boot": cmd_boot, "list": cmd_list,
        "destroy": cmd_destroy, "flavors": cmd_flavors,
    }

    if args.command not in cmds:
        parser.print_help()
        sys.exit(1)
    cmds[args.command](args)
```

- [ ] **Step 5: Run all tests, fix any breakage from state change**

Run: `python3 test_cluster_tool.py -v`

The `TestTransactionalBoot.setUp` saves old-style state with `"snapshot"`. Update it to use `"flavors"`:

```python
ct.save_state({
    "flavors": {
        "default": {
            "source_cluster": "6ef80144",
            "source_primary_subnet": 135,
            "source_secondary_subnet": 153,
            "memory_kib": 67108864,
            "vcpus": 16,
            "disks": ["disk-0.qcow2"],
        },
    },
    "clones": {},
    "next_subnet": 160,
})
```

And `_boot_args` needs `--flavor`:

```python
def _boot_args(self):
    return argparse.Namespace(name="aabbccdd", flavor="default")
```

- [ ] **Step 6: Commit**

```bash
git add cluster-tool test_cluster_tool.py
git commit -m "cluster-tool: refactor state for multi-flavor support, add flavors command"
```

---

### Task 3: Rewrite snapshot command for multi-flavor

Replace the hardcoded snapshot with auto-detection from any source VM.

**Files:**
- Modify: `cluster-tool`

- [ ] **Step 1: Rewrite cmd_snapshot**

```python
def cmd_snapshot(args):
    state = load_state()
    flavor_name = args.name
    source_id = args.source

    if flavor_name in state.get("flavors", {}):
        sys.exit(f"Flavor '{flavor_name}' already exists. Delete it first with 'cluster-tool flavors --delete {flavor_name}'.")

    vm_name = f"test-infra-cluster-{source_id}-master-0"
    r = ssh_baremetal(f"virsh domstate {vm_name}")
    if r.stdout.strip() != "running":
        sys.exit(f"Source VM '{vm_name}' is '{r.stdout.strip()}', expected 'running'")

    print(f'Creating flavor "{flavor_name}" from {vm_name}...')
    fdir = flavor_dir(flavor_name)
    crypto = flavor_crypto_dir(flavor_name)

    print_step(1, 6, "Detecting VM specs")
    dominfo = ssh_baremetal(f"virsh dominfo {vm_name}").stdout
    specs = parse_dominfo(dominfo)
    dumpxml = ssh_baremetal(f"virsh dumpxml {vm_name}").stdout
    pool_name = f"test-infra-cluster-{source_id}"
    disk_infos = parse_disk_paths(dumpxml, pool_name)
    primary_net = ssh_baremetal(f"virsh net-dumpxml test-infra-net-{source_id}").stdout
    secondary_net = ssh_baremetal(f"virsh net-dumpxml test-infra-secondary-network-{source_id}").stdout
    specs["source_primary_subnet"] = parse_subnet(primary_net)
    specs["source_secondary_subnet"] = parse_subnet(secondary_net)
    # Get etcd image from the running cluster
    vm_ip = f"192.168.{specs['source_primary_subnet']}.{VM_IP_SUFFIX}"
    etcd_img = ssh_baremetal(
        f"ssh {' '.join(SSH_OPTS)} core@{vm_ip} "
        f"\"sudo grep -oP 'ETCD_IMAGE=\\K.*' /etc/kubernetes/manifests/etcd-pod.yaml 2>/dev/null || "
        f"sudo python3 -c \\\"import json; d=json.load(open('/etc/kubernetes/manifests/etcd-pod.yaml')); "
        f"print(next(e['value'] for c in d['spec']['containers'] for e in c.get('env',[]) if e.get('name')=='ETCD_IMAGE'))\\\"\"").stdout.strip()
    specs["etcd_image"] = etcd_img
    print_step(1, 6, "Detecting VM specs", "done")

    print_step(2, 6, "Extracting crypto keys")
    source_kc = f"/home/test/assisted-test-infra/build/kubeconfig/kubeconfig_test-infra-cluster-{source_id}"
    oc = f"KUBECONFIG={source_kc} oc --insecure-skip-tls-verify --server=https://{vm_ip}:6443"
    ssh_baremetal(f"mkdir -p {fdir} {crypto}")
    for secret, ns, dest in [
        ("loadbalancer-serving-signer", "openshift-kube-apiserver-operator", "lb-signer.key"),
        ("localhost-serving-signer", "openshift-kube-apiserver-operator", "localhost-signer.key"),
        ("service-network-serving-signer", "openshift-kube-apiserver-operator", "service-network-signer.key"),
        ("router-ca", "openshift-ingress-operator", "ingress.key"),
    ]:
        ssh_baremetal(f"{oc} extract secret/{secret} -n {ns} --keys=tls.key --to=- > {crypto}/{dest}")
    ssh_baremetal(f"{oc} extract configmap/admin-kubeconfig-client-ca -n openshift-config --keys=ca-bundle.crt --to=- > {crypto}/admin-kubeconfig-client-ca.crt")
    ingress_cn = ssh_baremetal(
        f"{oc} get secret/router-ca -n openshift-ingress-operator "
        f"-o jsonpath='{{.data.tls\\.crt}}' | base64 -d | openssl x509 -noout -subject | sed 's/subject=CN *= *//'")
    if not ingress_cn.stdout.strip():
        sys.exit("Failed to extract ingress operator CN")
    ssh_baremetal(f"echo '{ingress_cn.stdout.strip()}' > {crypto}/ingress-cn.txt")
    print_step(2, 6, "Extracting crypto keys", "done")

    print_step(3, 6, "Shutting down VM")
    ssh_baremetal(f"virsh shutdown {vm_name}")
    for _ in range(60):
        r = ssh_baremetal(f"virsh domstate {vm_name}")
        if "shut off" in r.stdout:
            break
        time.sleep(2)
    else:
        sys.exit("VM did not shut down in 2 minutes")
    print_step(3, 6, "Shutting down VM", "done")

    print_step(4, 6, f"Copying {len(disk_infos)} disk(s)")
    disk_names = []
    for i, disk in enumerate(disk_infos):
        dest_name = f"disk-{i}.qcow2"
        ssh_baremetal(f"cp --sparse=always {disk['path']} {fdir}/{dest_name}")
        ssh_baremetal(f"chown qemu:qemu {fdir}/{dest_name}")
        disk_names.append(dest_name)
    print_step(4, 6, f"Copying {len(disk_infos)} disk(s)", "done")

    print_step(5, 6, "Restarting source VM")
    ssh_baremetal(f"virsh start {vm_name}")
    print_step(5, 6, "Restarting source VM", "done")

    print_step(6, 6, "Saving metadata")
    specs["source_cluster"] = source_id
    specs["disks"] = disk_names
    specs["created_at"] = datetime.now(timezone.utc).isoformat()
    state["flavors"][flavor_name] = specs
    save_state(state)
    print_step(6, 6, "Saving metadata", "done")

    print(f'\nFlavor "{flavor_name}" created ({len(disk_names)} disk(s), {specs["memory_kib"]//1024//1024}GB RAM, {specs["vcpus"]} vCPUs)')
```

- [ ] **Step 2: Test manually**

Run: `./cluster-tool snapshot --name sno-64 --source 6ef80144`
Expected: Auto-detects specs, extracts crypto, copies disk(s), saves to `/data/cluster-tool/flavors/sno-64/`.
Verify: `cat ~/.cluster-tool/state.json` shows the flavor with detected specs.

- [ ] **Step 3: Commit**

```bash
git add cluster-tool
git commit -m "cluster-tool: rewrite snapshot for multi-flavor with auto-detection"
```

---

### Task 4: Rewrite boot command to use flavors

Update `cmd_boot` to read VM specs from the flavor metadata instead of hardcoded constants. Support multi-disk overlays.

**Files:**
- Modify: `cluster-tool`

- [ ] **Step 1: Update gen_vm_xml for dynamic specs and multi-disk**

Replace the current `gen_vm_xml` with:

```python
def gen_vm_xml(clone_id, overlay_disks, mac_primary, mac_secondary, memory_kib, vcpus):
    disk_xml = ""
    for i, overlay in enumerate(overlay_disks):
        dev = f"sd{chr(97+i)}"
        disk_xml += f"""
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{overlay}'/>
              <target dev='{dev}' bus='scsi'/>
            </disk>"""

    return textwrap.dedent(f"""\
        <domain type='kvm'>
          <name>test-infra-cluster-{clone_id}-master-0</name>
          <memory unit='KiB'>{memory_kib}</memory>
          <currentMemory unit='KiB'>{memory_kib}</currentMemory>
          <vcpu placement='static'>{vcpus}</vcpu>
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
            <emulator>/usr/libexec/qemu-kvm</emulator>{disk_xml}
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
```

- [ ] **Step 2: Update cmd_boot to use flavor metadata**

Key changes in `cmd_boot`:

```python
def cmd_boot(args):
    state = load_state()
    flavors = state.get("flavors", {})
    if not flavors:
        sys.exit("No flavors available. Run 'cluster-tool snapshot --name NAME --source ID' first.")

    flavor_name = args.flavor
    if not flavor_name:
        flavor_name = max(flavors, key=lambda k: flavors[k].get("created_at", ""))
    if flavor_name not in flavors:
        sys.exit(f"Flavor '{flavor_name}' not found. Available: {', '.join(flavors.keys())}")

    flavor = flavors[flavor_name]
    source_id = flavor["source_cluster"]
    src_primary = flavor["source_primary_subnet"]
    src_secondary = flavor["source_secondary_subnet"]
    memory_kib = flavor["memory_kib"]
    vcpus = flavor["vcpus"]
    disk_names = flavor["disks"]
    etcd_image = flavor.get("etcd_image", ETCD_IMAGE)
    fdir = flavor_dir(flavor_name)
    crypto = flavor_crypto_dir(flavor_name)

    clone_id = args.name or generate_clone_id()
    # ... rest uses these variables instead of constants ...
    # Create overlays for ALL disks:
    overlays = []
    for disk_name in disk_names:
        overlay = f"{REMOTE_OVERLAYS}/{clone_id}-{disk_name}"
        ssh_baremetal(f"qemu-img create -f qcow2 -b {fdir}/{disk_name} -F qcow2 {overlay}")
        ssh_baremetal(f"chown qemu:qemu {overlay}")
        cleanup.append(lambda o=overlay: ssh_baremetal(f"rm -f {o}", check=False))
        overlays.append(overlay)

    # VM XML uses dynamic specs:
    vm_xml = gen_vm_xml(clone_id, overlays, mac1, mac2, memory_kib, vcpus)

    # Recert uses source_id from flavor, not hardcoded:
    # --cn-san-replace api.test-infra-cluster-{source_id}.redhat.com:api.{domain}
    # etc.

    # Clone state records the flavor used:
    state["clones"][clone_id] = {
        "flavor": flavor_name,
        "subnet_primary": subnet,
        ...
    }
```

- [ ] **Step 3: Update destroy to handle multi-disk overlays**

```python
# In cmd_destroy, delete all overlays for the clone:
for disk_name in flavor.get("disks", ["disk-0.qcow2"]):
    ssh_baremetal(f"rm -f {REMOTE_OVERLAYS}/{cid}-{disk_name}", check=False)
```

- [ ] **Step 4: Update tests**

Update `TestTemplates.test_vm_xml` to match new signature:
```python
def test_vm_xml(self):
    xml = ct.gen_vm_xml("a1b2c3d4", ["/path/disk-0.qcow2", "/path/disk-1.qcow2"],
                         "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff", 67108864, 16)
    self.assertIn("<name>test-infra-cluster-a1b2c3d4-master-0</name>", xml)
    self.assertIn("/path/disk-0.qcow2", xml)
    self.assertIn("/path/disk-1.qcow2", xml)
    self.assertIn("sda", xml)
    self.assertIn("sdb", xml)
    self.assertIn("67108864", xml)
```

Update `TestTransactionalBoot._boot_args`:
```python
def _boot_args(self):
    return argparse.Namespace(name="aabbccdd", flavor="default")
```

- [ ] **Step 5: Run all tests**

Run: `python3 test_cluster_tool.py -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add cluster-tool test_cluster_tool.py
git commit -m "cluster-tool: boot from named flavors with dynamic VM specs and multi-disk"
```

---

### Task 5: Update list command to show flavor

- [ ] **Step 1: Update cmd_list to show flavor column**

```python
def cmd_list(args):
    state = load_state()
    clones = state.get("clones", {})
    if not clones:
        print("No clones running.")
        return
    print(f"{'NAME':<14s} {'FLAVOR':<15s} {'SUBNET':<22s} {'CREATED'}")
    print("-" * 70)
    for cid, info in clones.items():
        subnet = f"192.168.{info['subnet_primary']}.0/24"
        created = info.get("created_at", "?")[:19]
        flavor = info.get("flavor", "?")
        print(f"{cid:<14s} {flavor:<15s} {subnet:<22s} {created}")
```

- [ ] **Step 2: Commit**

```bash
git add cluster-tool
git commit -m "cluster-tool: show flavor in list output"
```

---

### Task 6: End-to-end test

- [ ] **Step 1: Full cycle with existing cluster**

```bash
# Create flavor from existing cluster
./cluster-tool snapshot --name sno-64 --source 6ef80144

# List flavors
./cluster-tool flavors

# Boot a clone
./cluster-tool boot --flavor sno-64 --name test1

# Add /etc/hosts entries (from the output)
# Test cluster access
KUBECONFIG=~/.kube/test1.kubeconfig oc get nodes

# List clones
./cluster-tool list

# Destroy
./cluster-tool destroy test1
```

- [ ] **Step 2: Verify multi-disk (if a source VM with extra disks exists)**

```bash
# If you have a VM with LVMS data disk:
./cluster-tool snapshot --name sno-lvms --source <lvms-cluster-id>
./cluster-tool flavors  # should show 2 disks
./cluster-tool boot --flavor sno-lvms --name lvms-test
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "cluster-tool: multi-flavor support verified end-to-end"
```
