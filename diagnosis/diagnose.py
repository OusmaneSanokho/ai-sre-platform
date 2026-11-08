import json
import os
import re
import textwrap
from datetime import datetime, timezone
 
EVIDENCE_DIR = "evidence"
EVIDENCE_FILE = os.path.join(EVIDENCE_DIR, "evidence.json")
DIAGNOSIS_FILE = os.path.join(EVIDENCE_DIR, "diagnosis.json")
 
IMAGE_PULL_REASONS = {"ErrImagePull", "ImagePullBackOff"}
AUTH_FAILURE_PATTERNS = (
    "password authentication failed",
    "authentication failed",
    "access denied for user",
)
CRASH_RESTART_THRESHOLD = 3
 
 
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
 
 
def rollout_impact(ev, app):
    dep = find_by_name(ev["deployments"], app)
    if dep and dep["desired_replicas"] > 0 and dep["ready_replicas"] >= dep["desired_replicas"]:
        return (
            f"The broken pods are new ones. The previous version of {app} is still "
            f"running, so users are probably not affected yet."
        )
    if dep:
        return (
            f"{app} has {dep['ready_replicas']} of {dep['desired_replicas']} copies ready. "
            f"Users are likely affected."
        )
    return f"{app} may be affected."
 
 
def exit_code_meaning(code):
    meanings = {
        0: "The app exited normally, but Kubernetes expects it to keep running.",
        1: "The app stopped because of an error inside it.",
        137: "The app was killed by the system, often because it used too much memory.",
        139: "The app crashed hard (segmentation fault).",
        143: "The app was asked to stop (SIGTERM).",
    }
    return meanings.get(code, "The app stopped with an error code.")
 
 
def is_crashing(pod):
    return (
        "CrashLoopBackOff" in pod["waiting_reasons"]
        or pod["restarts"] >= CRASH_RESTART_THRESHOLD
    )
 
 
def was_oom_killed(pod):
    return any(t["reason"] == "OOMKilled" for t in pod["terminations"])
 
 
def find_auth_failure_line(pod):
    for line in pod["error_log_lines"]:
        if any(p in line.lower() for p in AUTH_FAILURE_PATTERNS):
            return line.strip()
    return None
 
 
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
 
 
# ---------- rule 2: database login rejected ----------
 
def rule_database_auth_failure(ev):
    findings = []
    ns = ev["namespace"]
 
    # Step 1: find every pod that logs a rejected login.
    hits = []
    for pod in ev["pods"]:
        line = find_auth_failure_line(pod)
        if line:
            hits.append((pod, line))
 
    # Step 2: split them in two sides.
    # Client side: the line names another service ("I could not log in to postgres").
    # Server side: the line names no other service ("I rejected a login").
    clients = {}
    server_side = []
    for pod, line in hits:
        targets = [
            s for s in ev["services"]
            if s["name"] != pod["app"] and s["name"] in line
        ]
        if targets:
            entry = clients.setdefault(pod["app"], {"hits": [], "targets": []})
            entry["hits"].append((pod["name"], line))
            for t in targets:
                if t not in entry["targets"]:
                    entry["targets"].append(t)
        else:
            server_side.append((pod, line))
 
    used_server_pods = set()
 
    # Step 3: one finding per client app. Server-side lines from the
    # database it talks to become confirmation, not a separate problem.
    for app, entry in clients.items():
        targets = entry["targets"]
        target_apps = {(t["selector"] or {}).get("app") for t in targets}
        db_name = targets[0]["name"]
        db_up = all(t["ready_endpoints"] for t in targets)
 
        evidence = []
        for pod_name, line in entry["hits"]:
            evidence.append(fact(
                f"pod/{pod_name} logs",
                line,
                f"{app} reached the database, but the database rejected its login.",
            ))
 
        confirmed = False
        for pod, line in server_side:
            if pod["app"] in target_apps:
                confirmed = True
                used_server_pods.add(pod["name"])
                evidence.append(fact(
                    f"pod/{pod['name']} logs",
                    line,
                    "The database's own log confirms it rejected a login. "
                    "This is the same problem seen from the other side.",
                ))
 
        for t in targets:
            if t["ready_endpoints"]:
                evidence.append(fact(
                    f"service/{t['name']}",
                    f"{len(t['ready_endpoints'])} ready endpoint(s)",
                    "The database is up and answering. It is not down, it is refusing the credentials.",
                ))
            else:
                evidence.append(fact(
                    f"service/{t['name']}",
                    "no ready endpoints",
                    "The database also has no ready pod, so there may be a second problem.",
                ))
 
        dep = find_by_name(ev["deployments"], app)
        app_ready = (
            bool(dep)
            and dep["desired_replicas"] > 0
            and dep["ready_replicas"] >= dep["desired_replicas"]
        )
        if app_ready:
            evidence.append(fact(
                f"deployment/{app}",
                f"desired {dep['desired_replicas']}, ready {dep['ready_replicas']}",
                "Kubernetes thinks the app is healthy, because its readiness check "
                "does not test the database login.",
            ))
            impact = (
                f"Every request to {app} that needs {db_name} fails. Kubernetes still "
                f"shows {app} as healthy, so this will not show up in pod status."
            )
        else:
            impact = f"Requests to {app} that need {db_name} fail."
 
        if db_up and confirmed:
            confidence = "high"
            confidence_reason = (
                f"{app} says its login failed, {db_name} says it rejected a login, "
                f"and {db_name} is up. Both sides of the connection agree."
            )
        elif db_up:
            confidence = "high"
            confidence_reason = (
                "The logs say the login failed, and the database itself is up, "
                "so the problem is the credentials, not the database."
            )
        else:
            confidence = "medium"
            confidence_reason = (
                "The logs say the login failed, but the database is not fully up, "
                "so there may be more than one problem."
            )
 
        findings.append({
            "id": "database_auth_failure",
            "title": f"{app} cannot log in to {db_name}",
            "summary": (
                f"{app} is using the wrong credentials for {db_name}, so every request "
                f"that needs the database fails."
            ),
            "what_happened": (
                f"{app} can reach {db_name}, but {db_name} refuses its username or password. "
                f"Nothing is down: the network works and both sides are running. "
                f"This often happens after a password change, or a deploy that changed the config."
            ),
            "root_cause": f"Wrong database credentials used by {app}.",
            "impact": impact,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "evidence": evidence,
            "fix": {
                "command": (
                    f"check the DB_PASSWORD (or Secret) used by {app}. If a recent deploy "
                    f"changed it, roll back: kubectl rollout undo deployment/{app} -n {ns}"
                ),
                "verify": f"retry the failing request and check that {app} logs no more authentication errors",
            },
            "limits": (
                "The tool cannot see the real password. It only sees that the database "
                "rejected the login. It cannot tell which side is wrong: the app's "
                "password or the database user."
            ),
        })
 
    # Step 4: server-side rejections that no client explains.
    leftover_by_app = {}
    for pod, line in server_side:
        if pod["name"] not in used_server_pods:
            leftover_by_app.setdefault(pod["app"], []).append((pod["name"], line))
 
    for app, lines in leftover_by_app.items():
        evidence = []
        for pod_name, line in lines:
            evidence.append(fact(
                f"pod/{pod_name} logs",
                line,
                f"{app} rejected a login attempt.",
            ))
 
        findings.append({
            "id": "logins_rejected",
            "title": f"{app} is rejecting logins",
            "summary": f"Something is trying to log in to {app} with wrong credentials.",
            "what_happened": (
                f"{app} logged that it refused a login. No app in this namespace "
                f"reports the failure from its side, so the caller is unknown. It could "
                f"be an app in another namespace, a manual login attempt, or an old error."
            ),
            "root_cause": f"Unknown client using wrong credentials for {app}.",
            "impact": "unknown: no affected app found",
            "confidence": "low",
            "confidence_reason": (
                "Only one side of the connection is visible. The tool cannot tell who "
                "tried to log in."
            ),
            "evidence": evidence,
            "fix": {
                "command": f"look at the full logs: kubectl logs -n {ns} deploy/{app} --tail=50",
                "verify": "check if the rejected logins continue",
            },
            "limits": (
                "The tool only looks at one namespace. The client may be somewhere it cannot see."
            ),
        })
 
    return findings
 
 
# ---------- rule 3: image pull failure ----------
 
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
            "impact": rollout_impact(ev, app),
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
 
 
# ---------- rule 4: out of memory ----------
 
def rule_out_of_memory(ev):
    findings = []
    ns = ev["namespace"]
 
    oom_by_app = {}
    for pod in ev["pods"]:
        if was_oom_killed(pod):
            oom_by_app.setdefault(pod["app"], []).append(pod)
 
    for app, pods in oom_by_app.items():
        evidence = []
        limits = set()
 
        for pod in pods:
            for t in pod["terminations"]:
                if t["reason"] == "OOMKilled":
                    evidence.append(fact(
                        f"pod/{pod['name']} {t['when']} exit",
                        f"reason OOMKilled, exit code {t['exit_code']}",
                        "The system killed the container because it used more memory than allowed.",
                    ))
            evidence.append(fact(
                f"pod/{pod['name']}",
                f"restarts {pod['restarts']}",
                "Kubernetes restarts it, and it gets killed again.",
            ))
            limits.update(pod["memory_limits"].values())
 
        if limits:
            limit_text = ", ".join(sorted(limits))
            evidence.append(fact(
                f"deployment/{app} config",
                f"memory limit {limit_text}",
                "This is the maximum memory each container may use.",
            ))
        else:
            limit_text = "its limit"
 
        findings.append({
            "id": "out_of_memory",
            "title": f"{app} runs out of memory",
            "summary": f"{app} needs more memory than it is allowed, so the system keeps killing it.",
            "what_happened": (
                f"{app} is allowed {limit_text} of memory. When it goes over, the system "
                f"kills it on the spot. Kubernetes restarts it, it goes over again, and "
                f"gets killed again."
            ),
            "root_cause": f"Memory limit too low for {app} ({limit_text}).",
            "impact": rollout_impact(ev, app),
            "confidence": "high",
            "confidence_reason": "Kubernetes recorded OOMKilled as the reason the container stopped.",
            "evidence": evidence,
            "fix": {
                "command": (
                    f"raise resources.limits.memory for {app} in k8s/{app}.yaml and run "
                    f"kubectl apply -f k8s/, or roll back: kubectl rollout undo deployment/{app} -n {ns}"
                ),
                "verify": f"kubectl get pods -n {ns}  (restarts should stop increasing)",
            },
            "limits": (
                "The tool sees the kill and the limit, not how much memory the app "
                "really needs. Memory metrics (Prometheus, Phase 4) would show that."
            ),
        })
 
    return findings
 
 
# ---------- rule 5: crash loop ----------
 
def rule_crash_loop(ev):
    findings = []
    ns = ev["namespace"]
 
    crashing_by_app = {}
    for pod in ev["pods"]:
        if is_crashing(pod) and not was_oom_killed(pod):
            crashing_by_app.setdefault(pod["app"], []).append(pod)
 
    for app, pods in crashing_by_app.items():
        evidence = []
        crash_line = None
 
        for pod in pods:
            waiting = ", ".join(pod["waiting_reasons"]) or "none"
            evidence.append(fact(
                f"pod/{pod['name']}",
                f"restarts {pod['restarts']}, waiting: {waiting}",
                "The container keeps starting and stopping. Kubernetes waits longer "
                "between each restart.",
            ))
            for t in pod["terminations"]:
                evidence.append(fact(
                    f"pod/{pod['name']} {t['when']} exit",
                    f"reason {t['reason']}, exit code {t['exit_code']}",
                    exit_code_meaning(t["exit_code"]),
                ))
 
            logs = pod["previous_error_log_lines"] or pod["error_log_lines"]
            for line in logs[-3:]:
                evidence.append(fact(
                    f"pod/{pod['name']} logs before crash",
                    line.strip(),
                    "What the app printed just before it stopped.",
                ))
            if crash_line is None:
                for line in reversed(logs):
                    match = re.search(r"\w+(?:Error|Exception): .+", line)
                    if match:
                        crash_line = match.group(0).strip()
                        break
 
        if crash_line:
            cause = f"the app crashes at startup with: {crash_line}"
            confidence = "high"
            confidence_reason = "The app is restarting over and over, and its logs show the error that stops it."
        else:
            cause = "the app keeps exiting, but its logs do not show a clear error"
            confidence = "medium"
            confidence_reason = "The app is clearly crash-looping, but the logs do not name the error."
 
        what_happened = (
            f"Every time Kubernetes starts {app}, it stops again after a few seconds. "
            f"Kubernetes restarts it and waits a bit longer each time. This is called a crash loop."
        )
        if crash_line:
            what_happened += f" The last error printed was: {crash_line}."
 
        findings.append({
            "id": "crash_loop",
            "title": f"{app} is crash-looping",
            "summary": f"{app} crashes right after starting, again and again.",
            "what_happened": what_happened,
            "root_cause": f"In {app}, {cause}.",
            "impact": rollout_impact(ev, app),
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "evidence": evidence,
            "fix": {
                "command": (
                    f"read the error above. If it started after a recent change (config, "
                    f"environment variable, new image), roll back: "
                    f"kubectl rollout undo deployment/{app} -n {ns}"
                ),
                "verify": f"kubectl get pods -n {ns}  (status Running, restarts no longer increasing)",
            },
            "limits": (
                "The tool shows the last error the app printed. It cannot tell which "
                "recent change caused it. Comparing the config with the previous version would help."
            ),
        })
 
    return findings
 
 
RULES = [
    rule_dependency_scaled_to_zero,
    rule_database_auth_failure,
    rule_image_pull_failure,
    rule_out_of_memory,
    rule_crash_loop,
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