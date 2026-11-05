# AI SRE Platform

A Python tool that investigates Kubernetes incidents and explains the root cause with evidence.

Work in progress.

## Local setup

- Kubernetes cluster with kind (1 control-plane, 2 workers)
- Config: `cluster/kind-config.yaml`

Create the cluster:

    kind create cluster --config cluster/kind-config.yaml