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
  [snapshot]  ──── Golden disk image + CA signing keys
        |
        v
  [boot]  ──── qcow2 overlay (instant, copy-on-write)
        |            |
        v            v
   New libvirt    recert (new name, new certs,
   network        new IP, new hostname)
        |            |
        v            v
   HAProxy SNI    Fresh kubeconfig
   routing        with correct CA
        |
        v
   Working cluster accessible
   from your laptop
```

The tool uses the same certificate regeneration mechanism as Red Hat's [Image Based Install (IBI)](https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/edge_computing/image-based-installation-for-single-node-openshift) workflow, which deploys thousands of SNO clusters from a single seed image in production.

## Quick Start

```bash
# One-time: create a golden snapshot from your running SNO cluster
./cluster-tool snapshot

# Boot a fresh cluster (~5 min)
./cluster-tool boot --name my-test
# Follow the printed instructions to add /etc/hosts entries

# Use it
export KUBECONFIG=~/.kube/my-test.kubeconfig
oc get nodes

# Run smoke tests to verify everything works
./cluster-tool verify my-test

# Boot another cluster in parallel
./cluster-tool boot --name my-test-2

# See what's running
./cluster-tool list

# Tear down
./cluster-tool destroy my-test
./cluster-tool destroy --all
```

## Commands

| Command | Description |
|---------|-------------|
| `snapshot` | Create a golden snapshot from a running source cluster. Extracts the VM disk, CA signing keys, and ingress operator CN. |
| `boot [--name ID]` | Boot a fresh clone from the snapshot. Creates overlay disk, libvirt networks, runs recert, configures HAProxy, extracts kubeconfig. |
| `list` | Show all running clones with their subnets and creation times. |
| `verify ID` | Run smoke tests: deploy a pod that validates DNS, API access, image pulls, and service account tokens from inside the cluster. |
| `destroy ID\|--all` | Tear down a clone. Removes VM, networks, overlay disk, HAProxy entries. Works even if the clone isn't in state (handles orphans). |

## What Happens During Boot

1. **Create disk overlay** — qcow2 copy-on-write backed by the golden snapshot. Instant, no 33GB copy.
2. **Create networks** — isolated libvirt NAT networks (primary + secondary) with DNS entries for API and apps hostnames.
3. **Boot VM** — define and start the clone VM with the overlay disk.
4. **Wait for SSH** — poll until the VM is reachable.
5. **Run recert** — stop kubelet/crio, start standalone etcd, run recert to regenerate all certificates and rename the cluster identity, configure dnsmasq overrides and nodeip hint, restart services.
6. **Wait for health** — poll `/healthz` until the API server is ready.
7. **Wait for operators** — poll all ClusterOperators until they are Available and not Degraded.
8. **Verify identity** — confirm the infrastructure resource has the correct API URL (prevents accidental source cluster corruption).
9. **Configure access** — extract kubeconfig, add HAProxy SNI entries, print /etc/hosts commands.

If any step fails, all previously created resources are rolled back automatically (transactional boot).

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
                                     │ HAProxy (SNI routing :6443/:443)  │
                                     │ Golden snapshot (qcow2)           │
                                     │ CA signing keys (crypto/)         │
                                     └───────────────────────────────────┘
```

- The tool runs on your laptop. All remote operations happen via SSH.
- Each clone gets its own isolated libvirt network with a unique subnet.
- HAProxy on the baremetal host routes API and ingress traffic to clones via SNI.
- State is tracked locally at `~/.cluster-tool/state.json`.

## Recert Integration

Each clone gets a fully unique identity through recert:

- **New cluster name** — `test-infra-cluster-<8-hex-chars>` (via `--cluster-rename`)
- **New certificates** — all certs regenerated with new keys
- **New IP address** — each clone on a unique subnet (via `--ip`, `--cn-san-replace`)
- **New hostname** — node name matches clone ID (via `--hostname`, `--cn-san-replace`)
- **Preserved CA signing keys** — the 4 kube-apiserver signing keys are preserved via `--use-key` so the kubeconfig's CA chain remains valid (matches the [lifecycle-agent](https://github.com/openshift-kni/lifecycle-agent) production pattern)
- **Full SAN replacement** — exact-match rules for `api.<domain>`, `api-int.<domain>`, `*.apps.<domain>`, hostname, and `system:node:<hostname>`
- **DNS configuration** — dnsmasq overrides and nodeip hint set via the official override mechanism (`/etc/default/sno_dnsmasq_configuration_overrides`)

## Prerequisites

- SSH access to the baremetal host (passwordless, as root)
- A running SNO cluster on the baremetal host (installed via [assisted-test-infra](https://github.com/openshift/assisted-test-infra))
- HAProxy configured with SNI routing on the baremetal host
- Python 3 on your laptop (stdlib only, no pip dependencies)
- `oc` CLI on the baremetal host

## Reliability

- **Transactional boot** — if any step fails, all created resources (overlay, networks, VM, HAProxy entries) are rolled back automatically.
- **State saved only on success** — no orphaned entries from failed boots.
- **Identity verification** — after recert, the tool confirms the clone's `apiServerURL` matches the expected domain before configuring external access.
- **Idempotent destroy** — works by clone ID alone, doesn't need state. Handles orphaned resources from crashed boots.
- **Operator health gate** — boot waits for ALL ClusterOperators to be Available and not Degraded before declaring the cluster ready.

## Roadmap

### Multi-Flavor Snapshots

Support multiple named snapshot flavors for different VM configurations:

```bash
# Snapshot different source VMs as named flavors
./cluster-tool snapshot --name sno-64 --source 6ef80144
./cluster-tool snapshot --name sno-cnv --source <cnv-cluster-id>
./cluster-tool snapshot --name sno-acm --source <acm-cluster-id>

# Boot from any flavor
./cluster-tool boot --flavor sno-64
./cluster-tool boot --flavor sno-cnv --name my-cnv-test

# List available flavors
./cluster-tool flavors
```

Each flavor stores its golden disk(s), crypto keys, and auto-detected VM specs (RAM, vCPUs, disk count). Multi-disk support for VMs with additional storage (e.g., LVMS data disks). See `docs/superpowers/plans/2026-05-04-multi-flavor.md`.

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
/cluster boot --flavor sno-64 --name agent-test
```

## Testing

```bash
# Run unit tests (20 tests)
python3 test_cluster_tool.py -v

# Run smoke tests on a live clone
./cluster-tool verify <clone-id>
```

Tests cover: state management, template generation, transactional rollback at every failure point, reverse cleanup order, CalledProcessError handling, recert flag verification, identity mismatch detection, and the success path.
