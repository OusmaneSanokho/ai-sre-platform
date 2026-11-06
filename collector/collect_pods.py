from kubernetes import client, config

NAMESPACE = "shop"


def main():
    config.load_kube_config()
    v1 = client.CoreV1Api()

    pods = v1.list_namespaced_pod(NAMESPACE)

    for pod in pods.items:
        name = pod.metadata.name
        phase = pod.status.phase
        containers = pod.status.container_statuses or []
        ready = all(c.ready for c in containers)
        restarts = sum(c.restart_count for c in containers)
        print(f"{name}  phase={phase}  ready={ready}  restarts={restarts}")


if __name__ == "__main__":
    main()