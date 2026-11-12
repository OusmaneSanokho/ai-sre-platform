import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from collector.collect import collect_evidence
from diagnosis.diagnose import diagnose
from evaluation.scenarios import EXPECTED_CONTEXT, NAMESPACE, SCENARIOS

POLL_SECONDS = 10          # how often we ask the judge while waiting
DETECT_TIMEOUT = 150       # give up on a scenario after this many seconds
HEALTHY_CHECK_DELAY = 20   # healthy scenario: wait this long, then ask once
COOL_DOWN_SECONDS = 15     # pause between "healthy again" and the next break
CLEAN_TIMEOUT = 240        # max wait for the cluster to be healthy again
RESULTS_DIR = os.path.join("evaluation", "results")

# Plain-language names for each rule id
RULE_LABELS = {
    None: "no problem",
    "dependency_scaled_to_zero": "a dependency scaled to zero",
    "database_auth_failure": "database login rejected",
    "logins_rejected": "logins rejected, caller unknown",
    "image_pull_failure": "image cannot be downloaded",
    "out_of_memory": "out of memory",
    "crash_loop": "crash loop",
}

STATUS_TEXT = {
    "correct": "CORRECT ✓",
    "correct_with_extras": "CORRECT, but with an extra finding !",
    "missed": "MISSED ✗",
    "false_alarm": "FALSE ALARM ✗",
}


# ---------- small helpers ----------

def label(rule_id):
    return RULE_LABELS.get(rule_id, rule_id)


def say(text="", end="\n"):
    print(text, end=end, flush=True)


def run_commands(commands):
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"command failed: {' '.join(cmd)}\n{result.stderr.strip()}"
            )


def check_context():
    """Refuse to run anywhere except the local lab cluster."""
    result = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    )
    context = result.stdout.strip()
    if context != EXPECTED_CONTEXT:
        say(f"Refusing to run: kubectl points to '{context}', not '{EXPECTED_CONTEXT}'.")
        say("This program breaks things on purpose, so it only runs on the lab cluster.")
        sys.exit(1)


def describe_findings(findings, still_waiting=True):
    if not findings:
        return "no problem found yet" if still_waiting else "no problem found"
    parts = []
    for f in findings:
        parts.append(f"\"{f['title']}\" ({label(f['id'])}), confidence {f['confidence']}")
    return "; ".join(parts)


# ---------- is the cluster healthy? ----------

def is_clean(ev):
    deployments = ev["deployments"]
    for d in deployments:
        if d["desired_replicas"] == 0:
            return False
        if d["ready_replicas"] < d["desired_replicas"]:
            return False

    expected_pods = sum(d["desired_replicas"] for d in deployments)
    if len(ev["pods"]) != expected_pods:
        return False

    return all(p["ready"] for p in ev["pods"])


def wait_until_clean():
    deadline = time.time() + CLEAN_TIMEOUT
    while time.time() < deadline:
        ev = collect_evidence(NAMESPACE, since_seconds=1)
        if is_clean(ev):
            return
        time.sleep(5)
    raise RuntimeError("the cluster did not come back to a healthy state in time")


# ---------- one scenario, one run ----------

def ask_judge(start):
    """Collect evidence from the start of this scenario only, then diagnose."""
    since = max(1, math.ceil(time.time() - start))
    ev = collect_evidence(NAMESPACE, since_seconds=since)
    return diagnose(ev)


def run_scenario(scenario, run_number):
    expected = scenario["expected"]
    result = {
        "scenario": scenario["name"],
        "title": scenario["title"],
        "run": run_number,
        "expected": expected,
        "status": None,
        "judge_answer": [],
        "confidence": None,
        "seconds_to_diagnose": None,
    }

    say("  Checking the cluster is healthy... ", end="")
    wait_until_clean()
    say("ok")

    say(f"  Waiting {COOL_DOWN_SECONDS} s so the previous scenario is fully over... ", end="")
    time.sleep(COOL_DOWN_SECONDS)
    say("ok")

    say("  Breaking it... ", end="")
    start = time.time()
    run_commands(scenario["break"])
    say("done" if scenario["break"] else "nothing to break")

    try:
        if expected is None:
            say(f"  Waiting {HEALTHY_CHECK_DELAY} s, then asking the judge once...")
            time.sleep(HEALTHY_CHECK_DELAY)
            findings = ask_judge(start)
            result["judge_answer"] = [f["title"] for f in findings]
            say(f"    judge answered: {describe_findings(findings, still_waiting=False)}")
            result["status"] = "correct" if not findings else "false_alarm"
            return result

        say(f"  Asking the judge every {POLL_SECONDS} s (max {DETECT_TIMEOUT} s)...")
        deadline = start + DETECT_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_SECONDS)
            findings = ask_judge(start)
            elapsed = round(time.time() - start)
            say(f"    after {elapsed} s: {describe_findings(findings)}")

            ids = [f["id"] for f in findings]
            result["judge_answer"] = [f["title"] for f in findings]

            if expected in ids:
                match = next(f for f in findings if f["id"] == expected)
                extras = [i for i in ids if i != expected]
                result["status"] = "correct" if not extras else "correct_with_extras"
                result["confidence"] = match["confidence"]
                result["seconds_to_diagnose"] = elapsed
                return result

        result["status"] = "missed"
        return result

    finally:
        say("  Repairing... ", end="")
        run_commands(scenario["restore"])
        say("done" if scenario["restore"] else "nothing to repair")


# ---------- final score ----------

def score_rows(results):
    rows = []
    for scenario in SCENARIOS:
        mine = [r for r in results if r["scenario"] == scenario["name"]]
        if not mine:
            continue
        clean = sum(1 for r in mine if r["status"] == "correct")
        extras = sum(1 for r in mine if r["status"] == "correct_with_extras")
        wrong = len(mine) - clean - extras
        times = [r["seconds_to_diagnose"] for r in mine if r["seconds_to_diagnose"] is not None]
        avg = round(sum(times) / len(times)) if times else None
        rows.append({
            "title": scenario["title"],
            "runs": len(mine),
            "clean": clean,
            "extras": extras,
            "wrong": wrong,
            "avg_seconds": avg,
        })
    return rows


def print_final_score(rows, results):
    total = len(results)
    good = sum(r["clean"] + r["extras"] for r in rows)
    extras = sum(r["extras"] for r in rows)

    say()
    say("=" * 20 + " FINAL SCORE " + "=" * 20)
    for r in rows:
        if r["wrong"]:
            mark = "✗"
        elif r["extras"]:
            mark = "!"
        else:
            mark = "✓"
        score = f"{r['clean'] + r['extras']}/{r['runs']}"
        avg = f"avg {r['avg_seconds']} s" if r["avg_seconds"] is not None else ""
        say(f"  {r['title']:<28} {score:>5}  {mark}   {avg}")
    say("  " + "-" * 49)
    say(f"  {'TOTAL':<28} {good}/{total} correct")
    if extras:
        say(f"  ({extras} of them also had an extra finding)")
    say("=" * 53)


def save_results(rows, results, started_at):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = started_at.strftime("%Y%m%d-%H%M%S")

    with open(os.path.join(RESULTS_DIR, f"run-{stamp}.json"), "w") as f:
        json.dump({"started_at": started_at.isoformat(), "results": results}, f, indent=2)

    total = len(results)
    good = sum(r["clean"] + r["extras"] for r in rows)

    lines = [
        "# Evaluation results",
        "",
        f"Run started: {started_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Each scenario breaks the lab cluster on purpose, then checks whether the "
        f"diagnosis finds the expected cause within {DETECT_TIMEOUT} seconds. "
        "The cluster is repaired after each scenario.",
        "",
        f"**Total: {good} / {total} correct diagnoses**",
        "",
        "| Scenario | Runs | Correct | Correct + extra finding | Missed or false alarm | Avg time to diagnose |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        avg = f"{r['avg_seconds']} s" if r["avg_seconds"] is not None else "-"
        lines.append(
            f"| {r['title']} | {r['runs']} | {r['clean']} | {r['extras']} | {r['wrong']} | {avg} |"
        )

    lines += ["", "## Details", "",
              "| Scenario | Run | Expected | Judge answered | Result | Time |",
              "|---|---|---|---|---|---|"]
    for r in results:
        answer = "; ".join(r["judge_answer"]) or "nothing"
        when = f"{r['seconds_to_diagnose']} s" if r["seconds_to_diagnose"] is not None else "-"
        lines.append(
            f"| {r['title']} | {r['run']} | {label(r['expected'])} | {answer} | "
            f"{STATUS_TEXT[r['status']]} | {when} |"
        )

    report = "\n".join(lines) + "\n"
    for name in (f"run-{stamp}.md", "latest.md"):
        with open(os.path.join(RESULTS_DIR, name), "w") as f:
            f.write(report)

    return os.path.join(RESULTS_DIR, "latest.md")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Break the lab on purpose and score the diagnosis.")
    parser.add_argument("--runs", type=int, default=1, help="how many times to run each scenario")
    parser.add_argument("--only", nargs="*", help="run only these scenarios (by name)")
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            say(f"{s['name']:<20} {s['title']:<28} {s['description']}")
        return

    selected = SCENARIOS
    if args.only:
        known = {s["name"] for s in SCENARIOS}
        unknown = [n for n in args.only if n not in known]
        if unknown:
            say(f"Unknown scenario(s): {', '.join(unknown)}. Use --list to see them.")
            sys.exit(1)
        selected = [s for s in SCENARIOS if s["name"] in args.only]

    check_context()
    started_at = datetime.now(timezone.utc)
    results = []
    count = len(selected)

    for run_number in range(1, args.runs + 1):
        for i, scenario in enumerate(selected, start=1):
            say()
            say(f"Scenario {i}/{count} (run {run_number}/{args.runs}): {scenario['title']}")
            say(f"  What we break:  {scenario['description']}")
            say(f"  Right answer:   {label(scenario['expected'])}")

            result = run_scenario(scenario, run_number)
            results.append(result)

            when = f"  ({result['seconds_to_diagnose']} s)" if result["seconds_to_diagnose"] is not None else ""
            say(f"  Result: {STATUS_TEXT[result['status']]}{when}")

    say()
    say("Making sure the cluster is healthy before finishing... ", end="")
    wait_until_clean()
    say("ok")

    rows = score_rows(results)
    print_final_score(rows, results)
    path = save_results(rows, results, started_at)
    say(f"Full report saved to {path}")


if __name__ == "__main__":
    main()