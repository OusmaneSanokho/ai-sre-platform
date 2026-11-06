import json
import os
from datetime import datetime, timezone

from kubernetes import client, config

NAMESPACE = "shop"
OUTPUT_DIR = "evidence"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "evidence.json")


def collect_pods(core):
    pods = []
    for pod in core.list_namespaced_pod(NAMESPACE).items:
        containers = pod.status.container_statuses or []

        waiting_reasons = []
        for c in containers:
            if c.state and c.state.waiting:
                waiting_reasons.append(c.state.waiting.reason)

        pods.append({
            "name": pod.metadata.name,
            "app": (pod.metadata.labels or {}).get("app"),
            "phase": pod.status.phase,
            "ready": bool(containers) and all(c.ready for c in containers),
            "restarts": sum(c.restart_count for c in containers),
            "waiting_reasons": waiting_reasons,
            "created_at": pod.metadata.creation_timestamp.isoformat(),
        })
    return pods


def collect_deployments(apps):
    deployments = []
    for d in apps.list_namespaced_deployment(NAMESPACE).items:
        deployments.append({
            "name": d.metadata.name,
            "desired_replicas": d.spec.replicas,
            "ready_replicas": d.status.ready_replicas or 0,
        })
    return deployments


def collect_services(core, discovery):
    services = []
    for svc in core.list_namespaced_service(NAMESPACE).items:
        name = svc.metadata.name

        slices = discovery.list_namespaced_endpoint_slice(
            NAMESPACE,
            label_selector=f"kubernetes.io/service-name={name}",
        ).items

        ready_ips = []
        for s in slices:
            for ep in s.endpoints or []:
                if ep.conditions and ep.conditions.ready:
                    ready_ips.extend(ep.addresses)

        services.append({
            "name": name,
            "selector": svc.spec.selector,
            "ready_endpoints": ready_ips,
        })
    return services


def collect_warning_events(core):
    events = []
    for e in core.list_namespaced_event(NAMESPACE).items:
        if e.type != "Warning":
            continue

        when = e.last_timestamp or e.event_time
        events.append({
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "reason": e.reason,
            "message": e.message,
            "count": e.count,
            "last_seen": when.isoformat() if when else None,
        })
    return events


def main():
    config.load_kube_config()
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    discovery = client.DiscoveryV1Api()

    evidence = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "namespace": NAMESPACE,
        "pods": collect_pods(core),
        "deployments": collect_deployments(apps),
        "services": collect_services(core, discovery),
        "warning_events": collect_warning_events(core),
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(evidence, f, indent=2)

    print(f"Evidence saved to {OUTPUT_FILE}")
    print(f"  pods:           {len(evidence['pods'])}")
    print(f"  deployments:    {len(evidence['deployments'])}")
    print(f"  services:       {len(evidence['services'])}")
    print(f"  warning events: {len(evidence['warning_events'])}")


if __name__ == "__main__":
    main()