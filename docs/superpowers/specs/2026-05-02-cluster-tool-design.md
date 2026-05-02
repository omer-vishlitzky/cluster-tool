# cluster-tool: Snapshot-based SNO Cluster Cloning

## Problem

Installing an OpenShift SNO cluster via assisted-test-infra takes 30-45+ minutes. For assisted-installer development, fresh clusters are needed frequently and sometimes in parallel. There is no way to quickly spin up a pre-configured cluster from a known-good state.

## Solution

A CLI tool that snapshots a running SNO cluster's VM disk, then boots N independent clones from that snapshot using qcow2 copy-on-write overlays. Each clone gets a unique identity (cluster name, certificates, IP, hostname) via the `recert` tool, and is made accessible from the developer's local machine through HAProxy SNI routing and /etc/hosts entries.

## Architecture

```
Developer laptop                     Baremetal (rdu-infra-edge-07, 10.1.155.16)
+---------------+       SSH          +-------------------------------------------+
| cluster-tool  |--------------------| libvirt VMs                               |
| (Python)      |                    |                                           |
|               |                    | +-------------+  +-------------+          |
| Writes:       |                    | | clone-a1b2  |  | clone-c3d4  |          |
| ~/.kube/      |                    | | .160.10     |  | .161.10     |          |
| /etc/hosts    |                    | +-------------+  +-------------+          |
+---------------+                    |                                           |
                                     | HAProxy (SNI routing on :6443, :443, :80) |
                                     | Golden snapshot (qcow2, ~33GB)            |
                                     | Overlays + golden snapshot stored here     |
                                     +-------------------------------------------+
```

## CLI Interface

```
cluster-tool snapshot               # One-time: create golden image from running cluster
cluster-tool boot [--name NAME]     # Boot a fresh cluster (~2 min)
cluster-tool list                   # Show running clones
cluster-tool destroy NAME           # Tear down a clone
cluster-tool destroy --all          # Tear down all clones
```

Requires `sudo` for local `/etc/hosts` writes.

## Infrastructure Specifics

### Source Cluster (6ef80144)

| Detail | Value |
|--------|-------|
| Baremetal IP | `10.1.155.16` |
| Baremetal host | `rdu-infra-edge-07.infra-edge.lab.eng.rdu2.redhat.com` |
| Cluster naming pattern | `test-infra-cluster-<8-hex-chars>` |
| Base domain pattern | `test-infra-cluster-<ID>.redhat.com` |
| Node naming pattern | `test-infra-cluster-<ID>-master-0` |
| OCP version | 4.19.27 |
| VM specs | 64GB RAM, 16 vCPU, 33GB qcow2 disk |
| VM disk path | `/data/test/assisted-test-infra/storage_pool/test-infra-cluster-6ef80144/test-infra-cluster-6ef80144-master-0-disk-0` |
| SSH access | `core@<VM-IP>` from baremetal via root's SSH key |
| Kubeconfig on VM | `/etc/kubernetes/static-pod-resources/kube-apiserver-certs/secrets/node-kubeconfigs/lb-ext.kubeconfig` |
| Recert image | `quay.io/edge-infrastructure/recert:latest` (pre-pulled on VM) |
| Two NICs | Primary (`test-infra-net-*`) + secondary (`test-infra-secondary-network-*`) |

### Subnet Layout

Primary networks use `192.168.127-135.0/24`. Secondary networks use `192.168.145-153.0/24` (offset of ~18 from their primary). Clone subnets start at `192.168.160.0/24` for primary and `192.168.178.0/24` for secondary, incrementing per clone.

### Recert Configuration Per Clone

```
--cluster-rename test-infra-cluster-<NEW-ID>:test-infra-cluster-<NEW-ID>.redhat.com
--hostname test-infra-cluster-<NEW-ID>-master-0
--ip 192.168.<X>.10
--cn-san-replace test-infra-cluster-6ef80144:test-infra-cluster-<NEW-ID>
--cn-san-replace 192.168.135.10:192.168.<X>.10
```

## Per-Clone Resources

### Libvirt Networks

Each clone gets two libvirt networks matching the original VM's dual-NIC setup.

**Primary network** (`test-infra-net-<NEW-ID>`):
- Subnet: `192.168.<X>.0/24`
- Bridge: `ct<N>` (unique short name)
- DHCP reservation: `<NEW-MAC>` -> `192.168.<X>.10`
- DNS entries for `api.`, `api-int.`, `*.apps.` hostnames

**Secondary network** (`test-infra-secondary-network-<NEW-ID>`):
- Subnet: `192.168.<X+18>.0/24`
- Bridge: `sct<N>` (unique short name)
- DHCP reservation: `<NEW-MAC-2>` -> `192.168.<X+18>.10`
- Same DNS entries as primary

### HAProxy

Added to all three frontends on baremetal (`/etc/haproxy/haproxy.cfg`):

```
# frontend api (:6443) - SNI routing
use_backend api-<NEW-ID> if { req_ssl_sni -m end test-infra-cluster-<NEW-ID>.redhat.com }

backend api-<NEW-ID>
    server api 192.168.<X>.10:6443 check

# frontend ingress-https (:443) - SNI routing
use_backend ingress-https-<NEW-ID> if { req_ssl_sni -m end test-infra-cluster-<NEW-ID>.redhat.com }

backend ingress-https-<NEW-ID>
    server ingress 192.168.<X>.10:443 check

# frontend ingress-http (:80) - Host header routing
use_backend ingress-http-<NEW-ID> if { hdr_end(host) test-infra-cluster-<NEW-ID>.redhat.com }

backend ingress-http-<NEW-ID>
    mode http
    server ingress 192.168.<X>.10:80 check
```

### Local /etc/hosts

All entries point to `10.1.155.16` (baremetal HAProxy):

```
# cluster-tool: test-infra-cluster-<NEW-ID>
10.1.155.16 api.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 console-openshift-console.apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 oauth-openshift.apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 downloads-openshift-console.apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 alertmanager-main-openshift-monitoring.apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 prometheus-k8s-openshift-monitoring.apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 prometheus-k8s-federate-openshift-monitoring.apps.test-infra-cluster-<NEW-ID>.redhat.com
10.1.155.16 thanos-querier-openshift-monitoring.apps.test-infra-cluster-<NEW-ID>.redhat.com
```

## Boot Sequence

```
 1. Generate NEW-ID (8 random hex chars)
 2. Allocate subnet (next free from 192.168.160+)
 3. SSH to baremetal:
    a. qemu-img create -f qcow2 -b <snapshot> -F qcow2 <overlay>
    b. virsh net-define + virsh net-start (primary + secondary)
    c. virsh define + virsh start (VM with two NICs)
 4. Wait for SSH (poll core@192.168.<X>.10 via baremetal jump)
 5. SSH to VM (through baremetal):
    a. sudo systemctl stop kubelet
    b. sudo systemctl stop crio
    c. Start standalone etcd from /var/lib/etcd
    d. sudo podman run recert with config JSON
    e. Stop standalone etcd
    f. sudo systemctl start crio
    g. sudo systemctl start kubelet
 6. Wait for API server healthy (poll /healthz via curl on baremetal)
 7. SCP lb-ext.kubeconfig, rewrite server URL to
    api.test-infra-cluster-<NEW-ID>.redhat.com:6443
 8. SSH to baremetal: append to /etc/haproxy/haproxy.cfg, reload haproxy
 9. Local: append to /etc/hosts (sudo)
10. Print kubeconfig path + export command
```

## Destroy Sequence

```
1. SSH to baremetal:
   a. virsh destroy + virsh undefine <VM>
   b. virsh net-destroy + virsh net-undefine (primary + secondary)
   c. rm <overlay-disk>
   d. Remove HAProxy entries from config, reload
2. Local: remove /etc/hosts block (sudo)
3. Remove local kubeconfig file
4. Update ~/.cluster-tool/state.json
```

## Snapshot Sequence

```
1. SSH to baremetal:
   a. virsh shutdown test-infra-cluster-6ef80144-master-0
   b. Wait for VM to stop
   c. cp <disk> /root/.cluster-tool/golden-snapshot.qcow2
   d. virsh start test-infra-cluster-6ef80144-master-0
2. Record snapshot metadata in state.json
```

## State File

`~/.cluster-tool/state.json` on the local machine:

```json
{
  "snapshot": {
    "source_cluster": "6ef80144",
    "source_disk": "/data/test/assisted-test-infra/storage_pool/test-infra-cluster-6ef80144/test-infra-cluster-6ef80144-master-0-disk-0",
    "golden_snapshot": "/root/.cluster-tool/golden-snapshot.qcow2",
    "created_at": "2026-05-02T14:00:00Z"
  },
  "clones": {
    "a1b2c3d4": {
      "subnet_primary": 160,
      "subnet_secondary": 178,
      "vm_name": "test-infra-cluster-a1b2c3d4-master-0",
      "overlay_disk": "/root/.cluster-tool/overlays/a1b2c3d4.qcow2",
      "created_at": "2026-05-02T14:05:00Z"
    }
  },
  "next_subnet": 161
}
```

## Implementation

Single Python file: `cluster-tool`. Runs locally, all remote operations via `subprocess.run(["ssh", ...])`. No external Python dependencies beyond stdlib.

## Constraints

- SNO only (recert limitation).
- `clusterNetwork` (172.30.0.0/16) and `serviceNetwork` (10.128.0.0/14) are shared across all clones. This is fine since each clone is on an isolated libvirt network.
- Maximum ~30 clones before exhausting the 192.168.160-190 subnet range.
- Each clone uses 64GB RAM and 16 vCPU. The baremetal machine's capacity determines how many can run in parallel.

## Out of Scope

- Multi-node clusters (recert is SNO-only).
- Changing the OCP version of clones.
- Full IBI workflow (overkill for this use case).
- Persistent storage or PV management across clones.
