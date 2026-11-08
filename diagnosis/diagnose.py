import json
import os
import re
import textwrap
from datetime import datetime, timezone

EVIDENCE_DIR = "evidence"
EVIDENCE_FILE = os.path.join(EVIDENCE_DIR, "evidence.json")
DIAGNOSIS_FILE = os.path.join(EVIDENCE_DIR, "diagnosis.json")

IMAGE_PULL_REASONS = {"ErrImagePull", "ImagePullBackOff"}


# ---------- helpers ----------

def load_evidence(path):
    with open(path) as f:
        return json.load(f)


def find_by_name(items, name):
    for item in items:
        if item["name"] == name:
            return item
    return None


def fact(source, observed, meaning):
    return {"source": source, "observed": observed, "meaning": meaning}


# ---------- rule 1: dependency scaled to zero ----------

def rule_dependency_scaled_to_zero(ev):
    findings = []
    ns = ev["namespace"]

    for svc in ev["services"]:
        if svc["ready_endpoints"]:
            continue

        app = (svc["selector"] or {}).get("app")
        dep = find_by_name(ev["deployments"], app)
        if dep is None or dep["desired_replicas"] != 0:
            continue

        victims = []
        for pod in ev["pods"]:
            for line in pod["error_log_lines"]:
                if svc["name"] in line:
                    victims.append((pod["name"], pod["app"], line.strip()))
                    break

        affected = sorted({victim_app for _, victim_app, _ in victims})

        evidence = [
            fact(
                f"deployment/{dep['name']}",
                f"desired {dep['desired_replicas']}, ready {dep['ready_replicas']}",
                "Someone or something asked for zero copies. This is not a crash: "
                "a crash would show desired 1, ready 0.",
            ),
            fact(
                f"service/{svc['name']}",
                "no ready endpoints",
                "The Service still exists, but has no pod to forward calls to.",
            ),
        ]
        for pod_name, victim_app, line in victims:
            evidence.append(fact(
                f"pod/{pod_name} logs",
                line,
                f"{victim_app} tried to reach {svc['name']} and failed.",
            ))

        what_happened = (
            f"The Deployment '{dep['name']}' is set to run 0 copies. "
            f"Its Service '{svc['name']}' still exists, but nobody is behind it, "
            f"so any app that calls '{svc['name']}' gets a connection error."
        )
        if affected:
            what_happened += f" This is already affecting: {', '.join(affected)}."
            impact = f"{', '.join(affected)} cannot reach {svc['name']}"
            confidence = "high"
            confidence_reason = (
                "Cause and effect are both visible: the deployment is at 0 "
                "and other pods log errors about it."
            )
        else:
            what_happened += (
                " No app has logged errors about it yet. Maybe nothing has "
                "called it since it was turned off."
            )
            impact = "no affected service seen in logs yet"
            confidence = "medium"
            confidence_reason = (
                "The cause is clear, but no other pod has logged errors about it yet."
            )

        findings.append({
            "id": "dependency_scaled_to_zero",
            "title": f"{dep['name']} scaled to zero",
            "summary": (
                f"{dep['name']} is turned off (0 copies), so anything that depends on it fails."
            ),
            "what_happened": what_happened,
            "root_cause": f"Deployment '{dep['name']}' is set to 0 replicas.",
            "impact": impact,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "evidence": evidence,
            "fix": {
                "command": f"kubectl scale deployment {dep['name']} -n {ns} --replicas=1",
                "verify": (
                    f"kubectl get pods -n {ns}  (a {dep['name']} pod should be Running), "
                    f"then retry the request that was failing."
                ),
            },
            "limits": (
                "The tool cannot tell who scaled it down or why. "
                "That would need the cluster audit log."
            ),
        })

    return findings


# ---------- rule 2: image pull failure ----------

def classify_pull_error(text):
    t = text.lower()
    missing = "not found" in t or "does not exist" in t
    auth = "unauthorized" in t or "authorization" in t or "denied" in t

    if missing and auth:
        return (
            "the image does not exist, or it is private and the cluster has no access",
            "medium",
            "The registry gives the same answer for a missing image and a private one, "
            "so the tool cannot tell which. Check the tag first: a typo or a version "
            "that was never built is the most common cause.",
        )
    if missing:
        return (
            "the image or tag does not exist in the registry",
            "high",
            "The registry clearly said the image or tag was not found.",
        )
    if auth:
        return (
            "the cluster is not allowed to pull this image",
            "high",
            "The registry refused access. The cluster probably needs registry credentials.",
        )
    return (
        "the image cannot be pulled",
        "low",
        "The error message does not say why. Check the image name, the tag and the network.",
    )


def rule_image_pull_failure(ev):
    findings = []
    ns = ev["namespace"]

    failing_by_app = {}
    for pod in ev["pods"]:
        reasons = IMAGE_PULL_REASONS.intersection(pod["waiting_reasons"])
        if reasons:
            failing_by_app.setdefault(pod["app"], []).append((pod, reasons))

    for app, failing in failing_by_app.items():
        evidence = []
        messages = []
        image = None

        for pod, reasons in failing:
            evidence.append(fact(
                f"pod/{pod['name']}",
                f"waiting: {', '.join(sorted(reasons))}",
                "The container cannot start because its image could not be downloaded.",
            ))
            for e in ev["warning_events"]:
                if e["object"] != f"Pod/{pod['name']}":
                    continue
                if e["message"].startswith("Error:"):
                    continue
                messages.append(e["message"])
                evidence.append(fact(
                    f"event on pod/{pod['name']}",
                    e["message"],
                    "This is the registry's answer when the node tried to download the image.",
                ))
                match = re.search(r'image "([^"]+)"', e["message"])
                if match and image is None:
                    image = match.group(1)

        cause, confidence, confidence_reason = classify_pull_error(" ".join(messages))
        if not messages:
            confidence = "medium"
            confidence_reason = "The pod is stuck on the image, but no event explains why."

        image_text = f"'{image}'" if image else "the requested image"

        dep = find_by_name(ev["deployments"], app)
        if dep and dep["ready_replicas"] >= dep["desired_replicas"]:
            impact = (
                f"The new version of {app} cannot start. The old version is still "
                f"running, so users are probably not affected yet."
            )
        else:
            impact = f"{app} has fewer ready copies than wanted. Users may be affected."

        findings.append({
            "id": "image_pull_failure",
            "title": f"image pull failure in {app}",
            "summary": f"A new version of {app} cannot start because its image cannot be downloaded.",
            "what_happened": (
                f"Kubernetes tried to start {app} with {image_text}. "
                f"The node did not have it locally, asked the registry, and failed. "
                f"Most likely cause: {cause}."
            ),
            "root_cause": f"Image {image_text} cannot be pulled: {cause}.",
            "impact": impact,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "evidence": evidence,
            "fix": {
                "command": (
                    f"fix the image tag in k8s/{app}.yaml and run kubectl apply -f k8s/  "
                    f"(or roll back: kubectl rollout undo deployment/{app} -n {ns})"
                ),
                "verify": f"kubectl get pods -n {ns}  (no {app} pod should be in ErrImagePull or ImagePullBackOff)",
            },
            "limits": (
                "The tool cannot see inside the registry. It only reads the error "
                "message the registry sent back."
            ),
        })

    return findings


RULES = [
    rule_dependency_scaled_to_zero,
    rule_image_pull_failure,
]


# ---------- report ----------

def wrap(text, indent="  "):
    return textwrap.fill(text, width=80, initial_indent=indent, subsequent_indent=indent)


def print_report(ev, findings):
    line = "=" * 60
    print(line)
    print(f"Diagnosis for namespace '{ev['namespace']}'")
    print(f"Evidence collected at {ev['collected_at']}")
    print(f"{len(findings)} problem(s) found")
    print(line)
    print()

    if not findings:
        print(wrap(
            "No known problem found. This only means none of the current rules "
            "matched. It does not prove the system is healthy."
        ))
        return

    for i, f in enumerate(findings, start=1):
        print(f"[{i}] {f['title'].upper()}")
        print(f"Confidence: {f['confidence']}")
        print(wrap(f["confidence_reason"]))
        print()
        print("Summary")
        print(wrap(f["summary"]))
        print()
        print("What happened")
        print(wrap(f["what_happened"]))
        print()
        print("Impact")
        print(wrap(f["impact"]))
        print()
        print("How we know")
        for e in f["evidence"]:
            print(wrap(f"- {e['source']}: {e['observed']}"))
            print(wrap(f"-> {e['meaning']}", indent="      "))
        print()
        print("What to do")
        print(wrap(f"Run:   {f['fix']['command']}"))
        print(wrap(f"Check: {f['fix']['verify']}"))
        print()
        print("Limits")
        print(wrap(f["limits"]))
        print()
        print("-" * 60)
        print()


def save_diagnosis(ev, findings):
    diagnosis = {
        "diagnosed_at": datetime.now(timezone.utc).isoformat(),
        "namespace": ev["namespace"],
        "evidence_collected_at": ev["collected_at"],
        "findings": findings,
    }
    with open(DIAGNOSIS_FILE, "w") as f:
        json.dump(diagnosis, f, indent=2)


def main():
    ev = load_evidence(EVIDENCE_FILE)

    findings = []
    for rule in RULES:
        findings.extend(rule(ev))

    print_report(ev, findings)
    save_diagnosis(ev, findings)
    print(f"Diagnosis saved to {DIAGNOSIS_FILE}")


if __name__ == "__main__":
    main()