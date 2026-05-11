# cluster-tool

Instant OpenShift SNO clusters from snapshots. Distribute them as OCI images.

## The Problem

Installing an OpenShift Single Node cluster takes 30-45 minutes. For development and testing, you need fresh clusters frequently — sometimes several in parallel, sometimes on different machines. Waiting 45 minutes each time is not acceptable.

## The Solution

`cluster-tool` snapshots a running SNO cluster's VM disk, then boots independent clones from that snapshot in ~5 minutes. Each clone gets a unique identity — new cluster name, new certificates, new IP, new hostname — via the [recert](https://github.com/rh-ecosystem-edge/recert) tool.

Flavors can be pushed to an OCI registry (Quay.io) and pulled on any baremetal machine. A new developer can go from nothing to a running OpenShift cluster with CNV, MCE, or LVMS pre-installed in ~20 minutes.

**Before:** 45 minutes to install a cluster from scratch.
**After:** ~5 minutes to boot a clone, ~20 minutes including pull from registry.

## Quick Start

```bash
# One-time setup: configure DNS on your laptop
sudo ./cluster-tool setup client

# Connect to a baremetal server (--data-path is where flavors and disk images are stored)
./cluster-tool connect myserver --host root@myhost.example.com --data-path /data/cluster-tool

# Create a snapshot from a running SNO cluster
# The source ID is the cluster ID from the VM name (e.g., test-infra-cluster-6ef80144-master-0 → 6ef80144)
./cluster-tool snapshot --name sno-64 --source 6ef80144

# Boot a fresh cluster (~5 min)
./cluster-tool boot --flavor sno-64 --name my-test

# Use it
export KUBECONFIG=~/.kube/my-test.kubeconfig
oc get nodes

# Push to Quay for distribution
./cluster-tool push sno-64 --registry quay.io/myorg/cluster-flavors --tag sno-64

# On another machine: pull and boot
./cluster-tool connect other-server --host root@other.example.com --data-path /home/cluster-tool
./cluster-tool pull --server other-server quay.io/myorg/cluster-flavors:sno-64
./cluster-tool boot --server other-server --flavor sno-64 --name remote-test

# Tear down
./cluster-tool destroy my-test
./cluster-tool destroy --server other-server remote-test
```

## Server Registry

cluster-tool manages multiple baremetal servers. Connect once, use by alias.

```bash
# Connect to servers (installs packages, configures storage)
./cluster-tool connect rdu --host root@rdu-host.example.com --data-path /data/cluster-tool
./cluster-tool connect dell --host root@dell-host.example.com --data-path /home/cluster-tool

# List connected servers
./cluster-tool servers

# Set a default server
./cluster-tool use rdu

# Target a specific server with --server
./cluster-tool boot --server dell --flavor sno-cnv --name my-test

# Without --server, uses the default
./cluster-tool boot --flavor sno-cnv --name my-test
```

For CI, use `local` as the host when running directly on the baremetal machine:
```bash
./cluster-tool connect ci --host local --data-path /home/cluster-tool
```

## Commands

| Command | Description |
|---------|-------------|
| `setup client` | One-time DNS setup on your laptop (dnsmasq + polkit). Requires sudo. |
| `connect NAME --host HOST [--data-path PATH]` | Connect to a baremetal server. Installs packages, configures storage, registers the server locally. |
| `servers` | List all connected servers with default marker. |
| `use NAME` | Set the default server. |
| `snapshot --name NAME --source ID` | Create a snapshot flavor from a running cluster. Flattens disks, extracts crypto keys, injects SSH key. The private key is stored in the flavor's `crypto/` dir and travels with push/pull. |
| `boot --flavor NAME [--name ID] [--server S]` | Boot a fresh clone. Creates overlays, networks, runs recert, waits for operators. |
| `list [--server S]` | Show running clones. |
| `flavors [--delete NAME] [--server S]` | List or delete flavors. |
| `verify ID [--server S]` | Run smoke tests on a clone. |
| `destroy ID\|--all [--server S]` | Tear down a clone. |
| `push NAME --registry REPO --tag TAG [--server S]` | Push a flavor to an OCI registry. Splits, compresses, builds multi-layer image. |
| `pull IMAGE [--name NAME] [--server S]` | Pull a flavor from an OCI registry. Downloads, decompresses, reassembles, registers. |

## OCI Distribution

Flavors are distributed as OCI container images via any registry (Quay.io, Docker Hub, etc.).

**Push** splits each disk into 1 GB chunks, compresses with pigz (parallel gzip), and builds an OCI image with each chunk as a separate layer. Layers download in parallel (20 concurrent streams).

**Pull** downloads the image, extracts chunks, decompresses in parallel (16 concurrent pigz processes), and reassembles the disks. The flavor is registered and ready for `boot`.

```bash
# Push (runs on the server where the flavor lives)
./cluster-tool push sno-cnv --registry quay.io/myorg/flavors --tag sno-cnv

# Pull (runs on any server)
./cluster-tool pull --server target quay.io/myorg/flavors:sno-cnv

# Boot from the pulled flavor
./cluster-tool boot --server target --flavor sno-cnv --name my-clone
```

Measured timings (SNO with CNV+LVMS, 90 GB disk):

| Operation | Time |
|-----------|------|
| Push to Quay | ~27 min |
| Pull from Quay | ~10 min |
| Boot with recert | ~5-7 min |
| **Pull + Boot (nothing to running cluster)** | **~15-20 min** |

## What Happens During Setup

### `setup client` (your laptop)
1. Installs dnsmasq
2. Configures NetworkManager to use dnsmasq as DNS backend
3. Grants your user polkit permission to reload NetworkManager without sudo
4. Verifies resolv.conf points to 127.0.0.1

### `connect` (baremetal server)
1. Installs libvirt, qemu-kvm, podman, pigz, haproxy
2. Configures HAProxy with SNI-based frontends for API (6443), ingress HTTPS (443), and HTTP (80). Sets SELinux boolean for non-standard ports. Opens firewall ports if firewalld is active.
3. Generates an ed25519 SSH keypair for cross-machine VM access
4. Configures podman parallel downloads (20 concurrent layers instead of the default 6, to saturate the network link when pulling large OCI images)
5. Auto-detects storage (largest partition) or uses `--data-path`, since baremetal machines have different disk layouts and the root filesystem is often too small for 60-90 GB disk images
6. Writes server config (idempotent — never overwrites existing config)
7. Registers the server alias on the client

## What Happens During Boot

1. **Create disk overlay(s)** — qcow2 copy-on-write backed by the golden snapshot(s). Instant.
2. **Create networks** — isolated libvirt NAT networks with DNS entries. Bridge names truncated to 15-char Linux limit.
3. **Boot VM** — define and start with the flavor's RAM and vCPU specs.
4. **Wait for SSH** — poll until reachable using the flavor's SSH key.
5. **Run recert** — stop kubelet/crio, start standalone etcd (LCA-style, no `--force-new-cluster`), regenerate all certificates, rename cluster identity, fix PV node affinities, clean OVN/OVS state, configure dnsmasq and nodeip hint, restart services.
6. **Wait for health** — poll `/healthz` until API server is ready.
7. **Configure access** — extract kubeconfig, add HAProxy SNI entries, add dnsmasq DNS entry.
8. **Wait for operators** — poll all ClusterOperators until Available and not Degraded.
9. **Verify identity** — confirm apiServerURL matches expected domain.

If any step fails, all resources are rolled back automatically (transactional boot).

## What Happens During Snapshot

1. **Detect etcd image** — read from the running cluster's pod manifest.
2. **Extract crypto keys** — 4 CA signing keys + admin kubeconfig CA.
3. **Inject SSH key** — add cluster-tool's public key to the VM's authorized_keys for cross-machine boot.
4. **Copy SSH keypair** — store in the flavor's crypto dir (travels with push/pull).
5. **Prepare etcd for clean boot** — hardlink etcd cert files to prevent revision rollouts (see [Snapshot Recloning](#snapshot-recloning) below).
6. **Shut down VM** — graceful shutdown, wait for "shut off".
7. **Flatten disk(s)** — `qemu-img convert` produces standalone qcow2 (no backing file dependencies).
8. **Restart VM** — bring the source cluster back up.

## Architecture

```
Client (laptop)                      Server (baremetal)
┌──────────────────┐    SSH          ┌────────────────────────────────────┐
│ cluster-tool     │────────────────▶│ libvirt VMs                        │
│                  │                 │                                    │
│ ~/.config/       │                 │ ┌──────────┐  ┌──────────┐         │
│   cluster-tool/  │                 │ │ my-test  │  │ my-test-2│         │
│   servers.json   │                 │ │ .160.10  │  │ .161.10  │         │
│                  │                 │ └──────────┘  └──────────┘         │
└──────────────────┘                 │                                    │
                                     │ ~/.config/cluster-tool/            │
                                     │   state.json (flavors, clones)     │
                                     │   config (data path)               │
                                     │   cluster-tool.key (SSH key)       │
                                     │                                    │
                                     │ <data-path>/                       │
                                     │   flavors/ (golden snapshots)      │
                                     │   overlays/ (per-clone COW disks)  │
                                     │                                    │
                                     │ HAProxy (SNI routing :6443/:443)   │
                                     └────────────────────────────────────┘
```

- The tool runs on your laptop or directly on the server (`CLUSTER_TOOL_HOST=local`).
- State (flavors, clones, subnets) lives on the server.
- Client stores only the server registry (`servers.json`).
- Each clone gets its own isolated libvirt network with a unique subnet.
- HAProxy routes API and ingress traffic via SNI.

## Cross-Machine Boot

Flavors pushed to a registry can be pulled and booted on any machine. This works because:

- **Standalone disks** — `snapshot` flattens COW overlays with `qemu-img convert`. No backing file dependencies.
- **SSH key injection** — `snapshot` injects cluster-tool's SSH public key into the VM's `authorized_keys`. `boot` uses the matching private key (stored in the flavor's crypto dir) to SSH into the VM for recert.
- **Recert** — generates fresh certificates, cluster name, IP, and hostname. The booted cluster has no connection to the original.

## Reliability

- **Transactional boot** — if any step fails, all resources are rolled back in reverse order.
- **State saved only on success** — no orphaned entries from failed boots.
- **Identity verification** — after recert, confirms the clone's `apiServerURL` matches.
- **Idempotent destroy** — works by clone ID alone, handles orphaned resources.
- **Idempotent HAProxy** — strip-before-insert prevents duplicates.
- **Idempotent setup** — `connect` never overwrites existing server config (shared machines safe).
- **Operator health gate** — boot waits for ALL ClusterOperators.
- **File locking** — concurrent boot/destroy safe via `fcntl` locks.
- **Fail-fast** — no silent error suppression.

## Recert Integration

Each clone gets a fully unique identity through recert:

- **New cluster name** — `test-infra-cluster-<8-hex-chars>` (via `--cluster-rename`)
- **New certificates** — all certs regenerated with new keys
- **New IP address** — each clone on a unique subnet (via `--ip`)
- **New hostname** — node name matches clone ID (via `--hostname`, `--cn-san-replace`)
- **Preserved CA signing keys** — the 4 kube-apiserver signing keys are preserved via `--use-key`
- **Full SAN replacement** — exact-match rules for API, ingress, hostname, and system:node entries
- **PV node affinity fix** — PersistentVolumes with node affinity get their hostname replaced in etcd
- **DNS configuration** — dnsmasq overrides and nodeip hint set via the official override mechanism

## Snapshot Recloning

A snapshot can be booted as a clone, and that clone can be snapshotted again, creating a chain of arbitrary depth (clone-of-clone-of-clone...). Making this work requires handling two subsystems that carry stale state from the previous identity: OVN networking and etcd certificate revisions.

### OVN Networking Cleanup (boot time)

OVN-Kubernetes stores the node's tunnel endpoint IP, connection strings, and system identity in several databases on disk. When a clone boots with a new IP, these stale values prevent OVN from starting — the node stays `NotReady` with "no CNI configuration file."

During boot (after recert, before starting kubelet), the node fix script deletes:
- `/etc/openvswitch/conf.db` and its lock file — the OVS configuration database with the old IP in `ovn-encap-ip`
- `/etc/ovn/ovnsb_db.db` and `ovnnb_db.db` — OVN southbound/northbound databases
- `/var/lib/ovn-ic/etc/` — OVN interconnect state including stale certificates

Then it restarts the `openvswitch` service. On the next boot, `configure-ovs.sh` recreates `br-ex` from scratch, and `ovnkube-node` rebuilds its state with the new IP. The node reaches `Ready` in ~60 seconds.

This follows the same pattern used by the [lifecycle-agent](https://github.com/openshift/lifecycle-agent) (LCA) during Image-Based Install.

### etcd Certificate Revision Hardlinks (snapshot time)

OpenShift's etcd operator uses a revision system for safe rollouts. It keeps two copies of the TLS certificates on disk:

- `etcd-certs/secrets/etcd-all-certs/` — the live certs mounted by the running etcd pod
- `etcd-pod-{rev}/secrets/etcd-all-certs/` — a revision snapshot from the last successful rollout

The operator periodically compares the live certificates (via the Kubernetes API) against the revision snapshot. If they differ, it triggers a full revision rollout — redeploying the etcd static pod, which cascades to kube-apiserver and other components. This takes ~7 minutes.

**The problem with recloning:** recert regenerates all certificates by scanning the filesystem (`--crypto-dir`) and the etcd database (`--etcd-endpoint`) independently. It deduplicates certificates by content — if two files have identical bytes, recert treats them as one certificate and writes the same regenerated output to both. If the bytes differ, it treats them as separate certificates and generates independent replacements with different serial numbers.

On a first-generation clone (from the original installer), the live certs and revision snapshot are byte-identical. Recert deduplicates them and writes matching output. No rollout.

On a second-generation clone (clone-of-clone), the previous recert already wrote different bytes to each location (because it treated them as separate certs — the same problem, recursively). Now recert sees two different certificates and generates two different replacements. The operator detects the mismatch and triggers a 7-minute rollout.

**The fix:** during `snapshot`, before shutting down the VM, the tool replaces the cert files in `etcd-certs/secrets/etcd-all-certs/` with **hardlinks** to the corresponding files in `etcd-pod-{rev}/secrets/etcd-all-certs/`. Hardlinks are filesystem entries that point to the same physical data on disk (same inode). When recert scans both paths, it finds identical bytes (same inode = same data), deduplicates them into one certificate, and writes one regenerated output. Both paths see the same result. The operator finds no mismatch. No rollout.

Hardlinks (not symlinks) are required because the etcd pod mounts `etcd-certs/` as a container volume. A symlink pointing to `../../etcd-pod-24/` would resolve outside the mount boundary inside the container — the target wouldn't exist. Hardlinks have no path to resolve; they are direct inode references that work identically inside and outside containers.

The hardlinks survive `qemu-img convert` (block-level copy preserves filesystem metadata), qcow2 COW overlays (both directory entries reference the same inode through the overlay), and recert's write mechanism (`std::fs::write` uses `O_TRUNC` which modifies the existing inode in-place without breaking the link).

The etcd operator's cert-sync controller could theoretically break hardlinks via `renameat2(RENAME_EXCHANGE)`, but it has a content-equality check that skips the swap when disk content matches the API. Since recert ensures consistency, the check passes and the hardlinks survive.

This step also removes any stale `etcd-pod.yaml` from `etcd-certs/` — a file the etcd operator's installer pod creates during revision rollouts that would cause recert to crash on subsequent boots.

### Standalone etcd for Recert (boot time)

Recert needs a live etcd endpoint to read and write certificate data. During boot, after stopping kubelet and crio, the tool starts a standalone etcd container:

```
podman run -d --name etcd-recert \
  --network host --privileged \
  -v /var/lib/etcd:/store \
  --entrypoint etcd \
  <etcd-image> \
  --name editor --data-dir /store
```

The volume mount uses a different container path (`/store` instead of `/var/lib/etcd`) following the lifecycle-agent convention. The `--name editor` flag is ignored by etcd when existing WAL data is present — etcd loads its identity from the WAL metadata. No `--force-new-cluster` flag is used; that flag is designed for multi-member disaster recovery and is unnecessary on a single-node cluster.

### Forked Recert Image

We use `quay.io/rh-ee-ovishlit/recert:latest` with two fixes over upstream:

1. **Binary DER data handling** — upstream crashes on secrets with binary certificate data. The fork returns `None` for non-UTF-8 data instead of erroring.
2. **PersistentVolume node affinity** — upstream doesn't update PV node affinity hostnames during `--hostname`. The fork adds PV support in etcd encoding and hostname replacement.

## Prerequisites

- Python 3 (stdlib only, no pip dependencies)
- SSH access to the baremetal host (passwordless, as root)
- A running SNO cluster on the baremetal host

Everything else (libvirt, qemu-kvm, podman, pigz, haproxy) is installed automatically by `connect`.

## Testing

```bash
CLUSTER_TOOL_HOST=test@host python3 test_cluster_tool.py -v
```

Tests cover: state management, server registry, ExecutionEnv (local/SSH), template generation, multi-disk VM XML, transactional rollback, recert flags, identity verification, file locking, bridge name truncation, dnsmasq config, manifest build/parse, push command flow, pull command flow, snapshot with SSH key injection, snapshot etcd hardlinks, OVN cleanup during boot, standalone etcd configuration, setup client/server, connect/servers/use commands.
