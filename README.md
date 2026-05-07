# cluster-tool

Instant OpenShift SNO clusters from snapshots.

## The Problem

Installing an OpenShift Single Node cluster takes 30-45 minutes. For development and testing, you need fresh clusters frequently — sometimes several in parallel. Waiting 45 minutes each time is not acceptable.

## The Solution

`cluster-tool` snapshots a running SNO cluster's VM disk, then boots independent clones from that snapshot in under 5 minutes. Each clone gets a unique identity — new cluster name, new certificates, new IP, new hostname — via the [recert](https://github.com/rh-ecosystem-edge/recert) tool. Clones are fully independent and can run in parallel.

**Before:** 45 minutes to install a cluster from scratch.
**After:** ~5 minutes to boot a clone with full identity regeneration.

## How It Works

```
Source Cluster (installed once)
        |
        v
  [snapshot --name sno-64 --source <id>]
        |
        v
  Golden disk(s) + CA signing keys + auto-detected VM specs
  stored in /data/cluster-tool/flavors/<name>/
        |
        v
  [boot --flavor sno-64]
        |
        v
  qcow2 overlay(s) ──── recert (new name, new certs,
  (instant, COW)         new IP, new hostname)
        |                     |
        v                     v
   New libvirt            Fresh kubeconfig
   network                with correct CA
        |
        v
   HAProxy SNI + dnsmasq DNS
        |
        v
   Working cluster accessible from your laptop
```

The tool uses the same certificate regeneration mechanism as Red Hat's [Image Based Install (IBI)](https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/edge_computing/image-based-installation-for-single-node-openshift) workflow, which deploys thousands of SNO clusters from a single seed image in production.

## Quick Start

```bash
# One-time: create a named snapshot flavor from your running SNO cluster
./cluster-tool snapshot --name sno-64 --source 6ef80144

# See available flavors
./cluster-tool flavors

# Boot a fresh cluster (~5 min)
./cluster-tool boot --flavor sno-64 --name my-test

# Use it
export KUBECONFIG=~/.kube/my-test.kubeconfig
oc get nodes

# Run smoke tests to verify everything works
./cluster-tool verify my-test

# Boot another cluster in parallel (from the same or different flavor)
./cluster-tool boot --flavor sno-64 --name my-test-2

# See what's running
./cluster-tool list

# Tear down
./cluster-tool destroy my-test
./cluster-tool destroy --all
```

## Flavors

Flavors are named snapshot profiles. Each flavor captures a complete VM configuration — disk image(s), CA signing keys, and auto-detected specs (RAM, vCPUs, disk count, etcd image). Different source clusters produce different flavors.

```bash
# Snapshot different source VMs as named flavors
./cluster-tool snapshot --name sno-64 --source 6ef80144
./cluster-tool snapshot --name sno-cnv --source <cnv-cluster-id>
./cluster-tool snapshot --name sno-acm --source <acm-cluster-id>

# Boot from any flavor
./cluster-tool boot --flavor sno-64
./cluster-tool boot --flavor sno-cnv --name my-cnv-test

# List available flavors (shows RAM, CPUs, disk count)
./cluster-tool flavors

# Delete a flavor
./cluster-tool flavors --delete sno-cnv
```

Storage layout on the baremetal host:
```
/data/cluster-tool/
├── flavors/
│   ├── sno-64/
│   │   ├── disk-0.qcow2      # Golden OS disk (~63GB)
│   │   ├── disk-1.qcow2      # Extra disk (e.g., LVMS data), if present
│   │   └── crypto/            # CA signing keys (~28K)
│   └── sno-cnv/
│       ├── disk-0.qcow2
│       └── crypto/
└── overlays/                  # Per-clone COW overlays (~250MB-6GB each)
    ├── my-test-disk-0.qcow2
    └── my-test-disk-1.qcow2
```

VMs with multiple disks (e.g., LVMS data disks) are fully supported — all non-CDROM disks are auto-detected, snapshotted, and cloned.

## Commands

| Command | Description |
|---------|-------------|
| `snapshot --name NAME --source ID` | Create a named snapshot flavor from a running source cluster. Auto-detects VM specs, extracts disk(s), CA signing keys, and etcd image. |
| `boot --flavor NAME [--name ID]` | Boot a fresh clone from a flavor. Creates overlay disk(s), libvirt networks, runs recert, configures HAProxy and DNS, extracts kubeconfig. If `--flavor` is omitted, uses the most recently created flavor. |
| `flavors [--delete NAME]` | List all available flavors with their specs, or delete one. |
| `list` | Show all running clones with their flavor, subnet, and creation time. |
| `verify ID` | Run smoke tests: deploy a pod that validates DNS, API access, image pulls, and service account tokens from inside the cluster. |
| `destroy ID\|--all` | Tear down a clone. Removes VM, networks, overlay disk(s), HAProxy entries, DNS. Works even if the clone isn't in state. |

## What Happens During Boot

1. **Create disk overlay(s)** — qcow2 copy-on-write backed by the golden snapshot(s). Instant, no multi-GB copy.
2. **Create networks** — isolated libvirt NAT networks (primary + secondary) with DNS entries for API and apps hostnames. Each network gets an explicit bridge name (`br-<id>` / `bs-<id>`, truncated to the 15-char Linux limit).
3. **Boot VM** — define and start the clone VM with the overlay disk(s), using the flavor's RAM and vCPU specs.
4. **Wait for SSH** — poll until the VM is reachable.
5. **Run recert** — stop kubelet/crio, start standalone etcd (using the flavor's detected etcd image), run recert to regenerate all certificates, rename the cluster identity, fix PV node affinities, clear stale nodeip cache, daemon-reload, configure dnsmasq overrides and nodeip hint, restart services.
6. **Wait for health** — poll `/healthz` until the API server is ready.
7. **Configure access** — extract kubeconfig, add HAProxy SNI entries, add dnsmasq DNS entry.
8. **Wait for operators** — poll all ClusterOperators until they are Available and not Degraded.
9. **Verify identity** — confirm the infrastructure resource has the correct API URL (prevents accidental source cluster corruption).

If any step fails, all previously created resources are rolled back automatically (transactional boot).

## What Happens During Snapshot

1. **Detect etcd image** — read the etcd pod manifest from the running cluster.
2. **Extract crypto keys** — the 4 CA signing keys (lb-signer, localhost-signer, service-network-signer, ingress) and the admin kubeconfig CA.
3. **Shut down VM** — graceful shutdown, wait for "shut off".
4. **Create directories** — set up the flavor directory on the baremetal host.
5. **Copy disk(s)** — sparse copy of all detected disks to the flavor directory.
6. **Restart VM** — bring the source cluster back up.

VM specs (RAM, vCPUs, disk paths, subnets) are auto-detected from `virsh dominfo` and `virsh dumpxml` before the snapshot begins.

## Architecture

```
Your laptop                          Baremetal host
┌─────────────┐         SSH          ┌───────────────────────────────────┐
│ cluster-tool│─────────────────────▶│ libvirt VMs                       │
│  (Python)   │                      │                                   │
│             │                      │ ┌──────────┐  ┌──────────┐       │
│ Writes:     │                      │ │ my-test  │  │ my-test-2│       │
│ ~/.kube/    │                      │ │ .160.10  │  │ .161.10  │       │
│             │                      │ └──────────┘  └──────────┘       │
└─────────────┘                      │                                   │
                                     │ Flavors (golden snapshots)        │
                                     │ HAProxy (SNI routing :6443/:443)  │
                                     │ Overlays (per-clone COW disks)    │
                                     └───────────────────────────────────┘
```

- The tool runs on your laptop. All remote operations happen via SSH.
- Each clone gets its own isolated libvirt network with a unique subnet.
- HAProxy on the baremetal host routes API and ingress traffic to clones via SNI.
- State is tracked locally at `~/.cluster-tool/state.json`.

## Parallel Usage

Multiple `boot` and `destroy` operations can run concurrently. File locking (`fcntl`) protects shared resources:

- **State lock** (`~/.cluster-tool/state.lock`) — serializes subnet allocation and clone registration.
- **HAProxy lock** (`~/.cluster-tool/haproxy.lock`) — serializes reads and writes to `/etc/haproxy/haproxy.cfg`.

Each clone gets its own bridge name derived from its clone ID (`br-<id[:8]>` and `bs-<id[:8]>`), so parallel network creation never collides. Bridge names are truncated to 15 characters to stay within the Linux interface name limit.

HAProxy entries are inserted idempotently — existing entries for a clone ID are stripped before new ones are added, so a retry after a partial failure won't create duplicates.

Clone IDs that are substrings of each other (e.g., `demo` and `dev-env-demo`) are handled correctly — HAProxy backend names include the full clone ID, so stripping one clone's entries never touches another's.

## Clone-of-Clone Support

Snapshots work on clones too, not just original installations. You can snapshot a running clone to create a new flavor, then boot further clones from it.

When booting from a clone-of-clone snapshot, the tool:
- Clears the stale `/run/nodeip-configuration` cache (left over from the previous clone's identity)
- Runs `systemctl daemon-reload` before restarting kubelet (picks up unit file changes from recert)
- Restarts `nodeip-configuration` to bind to the new subnet

Without these steps, kubelet would start with the previous clone's IP address and the node would never become Ready.

## Recert Integration

Each clone gets a fully unique identity through recert:

- **New cluster name** — `test-infra-cluster-<8-hex-chars>` (via `--cluster-rename`)
- **New certificates** — all certs regenerated with new keys
- **New IP address** — each clone on a unique subnet (via `--ip`, `--cn-san-replace`)
- **New hostname** — node name matches clone ID (via `--hostname`, `--cn-san-replace`)
- **Preserved CA signing keys** — the 4 kube-apiserver signing keys are preserved via `--use-key` so the kubeconfig's CA chain remains valid (matches the [lifecycle-agent](https://github.com/openshift-kni/lifecycle-agent) production pattern)
- **Full SAN replacement** — exact-match rules for `api.<domain>`, `api-int.<domain>`, `*.apps.<domain>`, hostname, and `system:node:<hostname>`
- **PV node affinity fix** — PersistentVolumes with node affinity (e.g., LVMS/TopoLVM) have their hostname values replaced in etcd during recert, so LVMS volumes bind to the new node without API-level workarounds
- **DNS configuration** — dnsmasq overrides and nodeip hint set via the official override mechanism (`/etc/default/sno_dnsmasq_configuration_overrides`)

### Forked Recert Image

We use `quay.io/rh-ee-ovishlit/recert:latest` instead of the upstream `quay.io/edge-infrastructure/recert:latest`. The fork contains two fixes:

**Fix 1: Binary DER data handling.** Upstream recert crashes on secrets containing binary (DER-encoded) certificate data (e.g., Keycloak's `key.der`). The `process_byte_array_value` function in `json_crawl.rs` called `String::from_utf8(bytes)` with `.context()`, which turns non-UTF-8 data into a hard error. The fork changes this to return `None` for non-UTF-8 data — matching how the adjacent `process_data_url_value` function already handles binary content.

**Fix 2: PersistentVolume node affinity hostname replacement.** Upstream recert doesn't know about PersistentVolume resources in etcd. When `--hostname` is used, PVs with node affinity (created by LVMS/TopoLVM) still reference the old hostname, causing volumes to be unschedulable on the renamed node. The fork adds:
- PersistentVolume decoding/encoding support in `etcd_encoding.rs`
- A `fix_persistent_volumes` function in `hostname_rename/etcd_rename.rs` that walks all PVs and replaces old hostname values in `spec.nodeAffinity.required.nodeSelectorTerms[].matchExpressions[].values[]`

Source at `../recert-src/`.

## OSAC Integration

When using cluster-tool to boot clusters for [OSAC](https://github.com/openshift-assisted/osac-installer) development, the cloned cluster needs its operator subscriptions and routes refreshed. The `refresh-after-snapshot.sh` script (in the osac-installer repo, [PR #95](https://github.com/openshift-assisted/osac-installer/pull/95)) handles this:

```bash
# After booting a clone for OSAC development
export KUBECONFIG=~/.kube/my-test.kubeconfig
./refresh-after-snapshot.sh
```

The script deletes and recreates operator subscriptions (Authorino, AAP, Hive, MCE) so they re-resolve in the new cluster identity, and patches any routes that still reference the source cluster's domain.

## Prerequisites

- SSH access to the baremetal host (passwordless, as root)
- A running SNO cluster on the baremetal host (installed via [assisted-test-infra](https://github.com/openshift/assisted-test-infra))
- HAProxy configured with SNI routing on the baremetal host
- Python 3 on your laptop (stdlib only, no pip dependencies)
- `oc` CLI on the baremetal host
- dnsmasq (installed via package manager)

## One-Time Setup

Run once to enable automatic DNS resolution for cloned clusters:

```bash
./setup.sh
```

This configures dnsmasq as NetworkManager's DNS backend, grants your user permission to reload NM without sudo, and verifies everything works. Requires sudo during setup, never again after.

After this, `cluster-tool boot` handles DNS automatically — no manual `/etc/hosts` entries, no sudo.

## Reliability

- **Transactional boot** — if any step fails, all created resources (overlay, networks, VM, HAProxy entries) are rolled back automatically. The VM is waited on until fully dead before removing overlays.
- **State saved only on success** — no orphaned entries from failed boots.
- **Identity verification** — after recert, the tool confirms the clone's `apiServerURL` matches the expected domain before configuring external access.
- **Idempotent destroy** — works by clone ID alone, doesn't need state. Handles orphaned resources from crashed boots.
- **Idempotent HAProxy** — existing entries for a clone ID are stripped before insertion, so retries and re-boots never create duplicates.
- **Operator health gate** — boot waits for ALL ClusterOperators to be Available and not Degraded before declaring the cluster ready.
- **File locking** — concurrent boot/destroy operations are safe via `fcntl` locks on state and HAProxy.
- **Fail-fast** — no silent error suppression. SSH failures, subprocess errors, and unexpected states abort immediately with a clear message.

## Roadmap

### OSAC Internal Service URLs

OSAC components currently use external routes (e.g., `https://assisted-service.apps.<domain>:443`) to communicate with each other. These routes go through HAProxy and ingress, adding latency and a dependency on DNS/TLS for intra-cluster traffic. The correct pattern is to use internal service URLs (e.g., `http://assisted-service.assisted-installer.svc.cluster.local:8090`), which stay within the cluster network. This is a design improvement in osac-installer, not in cluster-tool, but it would eliminate the need for route patching after snapshot-based boots.

### Distributable Artifacts via OCI Registry

Push and pull flavors as OCI images via Quay.io:

```bash
# Push a flavor to the registry
./cluster-tool push sno-cnv

# On any machine: boot from a flavor (auto-pulls if not local)
./cluster-tool boot --flavor sno-cnv --name my-test

# List flavors available on the registry
./cluster-tool flavors --remote
```

The vision: a new user clones this repo, runs `./cluster-tool boot --flavor sno-cnv`, and gets a working cluster in minutes — the tool auto-pulls the golden image from Quay.io. Disk images are chunked into 1GB OCI layers for parallel transfer.

### Claude Code Skill

A Claude Code skill that lets any AI agent spawn clusters on demand:

```
/spawn-cluster boot --flavor sno-64 --name agent-test
```

The skill is prototyped at `~/.claude/skills/spawn-cluster/` and handles boot, list, verify, and destroy. Once cluster-tool is on PATH (symlinked to `~/.local/bin/cluster-tool`), the skill provides natural language access to cluster lifecycle.

## Testing

```bash
# Run unit tests (48 tests)
python3 test_cluster_tool.py -v

# Run smoke tests on a live clone
./cluster-tool verify <clone-id>
```

Tests cover: state management, template generation, multi-disk VM XML, transactional rollback at every failure point, reverse cleanup order, CalledProcessError handling, recert flag verification, identity mismatch detection, file locking, bridge name truncation, flavor state operations, dnsmasq config generation, and the success path.
