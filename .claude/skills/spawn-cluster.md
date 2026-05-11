---
name: spawn-cluster
description: Manage OpenShift SNO clusters — boot from snapshots (~5 min), snapshot running clusters, push/pull flavors to/from OCI registries, manage multiple baremetal servers. Use when you need a live cluster, want to distribute a cluster image, or manage the cluster lifecycle.
allowed-tools: Bash(cluster-tool *)
---

# Cluster Tool

Boot fresh OpenShift SNO clusters from golden snapshots in ~5 minutes. Each cluster gets a unique identity (certs, IP, hostname) and is fully independent. Distribute cluster images via OCI registries (Quay.io). Manage multiple baremetal servers.

## Commands

### Boot a cluster

```bash
cluster-tool boot --flavor <flavor> --name <name> [--server <server>]
```

- `--flavor` — which snapshot to boot from. Run `cluster-tool flavors` to see available ones.
- `--name` — a short identifier (e.g., `pr-1234`, `e2e`). Keep it under 8 chars.
- `--server` — which server to boot on. Omit to use the default server.

### Snapshot a running cluster

```bash
cluster-tool snapshot --name <flavor-name> --source <clone-id> [--server <server>]
```

Creates a reusable flavor from a running clone. Flattens disks, extracts crypto keys, injects SSH key, hardlinks etcd certs for clean recloning.

### Push a flavor to a registry

```bash
cluster-tool push <flavor> --registry <repo> --tag <tag> [--server <server>]
```

Splits disks into 1GB chunks, compresses with pigz, builds an OCI image, pushes to registry.

### Pull a flavor from a registry

```bash
cluster-tool pull <image> [--name <name>] [--server <server>]
```

Downloads, decompresses, reassembles disks, registers the flavor locally.

### Server management

```bash
cluster-tool connect <name> --host <user@host> --data-path <path>
cluster-tool servers
cluster-tool use <name>
```

Connect to baremetal servers. `connect` installs all dependencies (libvirt, qemu, podman, pigz, haproxy). `use` sets the default server.

### One-time client setup

```bash
sudo cluster-tool setup client
```

Installs dnsmasq for automatic DNS resolution of cluster domains.

### List / destroy / verify

```bash
cluster-tool flavors [--delete <name>]
cluster-tool list [--server <server>]
cluster-tool destroy <name> [--server <server>]
cluster-tool destroy --all [--server <server>]
cluster-tool verify <name> [--server <server>]
```

## After Boot

The tool prints the kubeconfig path. Use it:

```bash
export KUBECONFIG=~/.kube/<name>.kubeconfig
oc get nodes
oc get co
```

## Parallel Usage

Multiple boot/destroy commands can run in parallel safely. The tool uses file locking internally.

## What to tell the user

After booting, tell them:
1. The cluster name and flavor
2. The kubeconfig path (`~/.kube/<name>.kubeconfig`)
3. The `export KUBECONFIG=...` command to use it
4. That it takes ~5-9 minutes to be fully ready

After snapshot, tell them the flavor name and that they can push it with `cluster-tool push`.

After push, tell them the image reference (e.g., `quay.io/org/repo:tag`).

After pull, tell them the flavor is registered and ready for `boot`.

## Interpreting Arguments

If the user says:
- "spawn a cluster" / "I need a cluster" — run `boot` with a reasonable name
- "spawn 3 clusters" — run 3 `boot` commands in parallel with `&` and `wait`
- "tear down my clusters" / "clean up" — run `destroy --all`
- "what clusters are running" — run `list`
- "what flavors do we have" — run `flavors`
- "snapshot this cluster" / "save this as a flavor" — run `snapshot`
- "push this to quay" / "distribute this" — run `push`
- "pull the CNV flavor" / "get the cluster image" — run `pull`
- "connect to this machine" / "add a server" — run `connect`
- "which servers are connected" — run `servers`
