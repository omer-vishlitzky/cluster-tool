---
name: spawn-cluster
description: Spawn a fresh OpenShift SNO cluster in ~5 minutes from a pre-built snapshot. Use when you need a live cluster to test against, deploy something, run e2e tests, or validate a PR. Also use to list, destroy, or manage clusters.
allowed-tools: Bash(cluster-tool *)
---

# Spawn Cluster

Boot fresh OpenShift SNO clusters from golden snapshots in ~5 minutes. Each cluster gets a unique identity (certs, IP, hostname) and is fully independent.

## Commands

### Boot a cluster

```bash
cluster-tool boot --flavor <flavor> --name <name>
```

- `--flavor` — which snapshot to boot from. Run `cluster-tool flavors` to see available ones. If omitted, uses the most recent.
- `--name` — a short identifier for this cluster (e.g., `pr-1234`, `e2e`). Keep it under 8 chars to avoid bridge name issues. If omitted, a random hex ID is generated.

### List available flavors

```bash
cluster-tool flavors
```

### List running clusters

```bash
cluster-tool list
```

### Destroy a cluster

```bash
cluster-tool destroy <name>
```

### Destroy all clusters

```bash
cluster-tool destroy --all
```

### Verify a cluster is healthy

```bash
cluster-tool verify <name>
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
4. That it takes ~5 minutes to be fully ready

## Interpreting Arguments

If the user says:
- "spawn a cluster" / "I need a cluster" — run `boot` with a reasonable name
- "spawn 3 clusters" — run 3 `boot` commands in parallel with `&` and `wait`
- "tear down my clusters" / "clean up" — run `destroy --all`
- "what clusters are running" — run `list`
- "what flavors do we have" — run `flavors`
