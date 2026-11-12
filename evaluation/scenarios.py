NAMESPACE = "shop"
EXPECTED_CONTEXT = "kind-sre-lab"

UNDO_API = ["kubectl", "rollout", "undo", "deployment/orders-api", "-n", NAMESPACE]

# Each scenario says:
#   name:        short id used on the command line (--only)
#   title:       plain-language name shown in the output
#   description: what we break, in plain words
#   expected:    the rule id the diagnosis should find (None = nothing should be found)
#   break:       commands that cause the failure
#   restore:     commands that repair it
SCENARIOS = [
    {
        "name": "healthy",
        "title": "nothing broken",
        "description": "nothing is changed, the judge should find no problem",
        "expected": None,
        "break": [],
        "restore": [],
    },
    {
        "name": "db_scaled_to_zero",
        "title": "database turned off",
        "description": "postgres is scaled to 0 replicas",
        "expected": "dependency_scaled_to_zero",
        "break": [["kubectl", "scale", "deployment", "postgres", "-n", NAMESPACE, "--replicas=0"]],
        "restore": [["kubectl", "scale", "deployment", "postgres", "-n", NAMESPACE, "--replicas=1"]],
    },
    {
        "name": "wrong_db_password",
        "title": "wrong database password",
        "description": "orders-api gets DB_PASSWORD=wrong",
        "expected": "database_auth_failure",
        "break": [["kubectl", "set", "env", "deployment/orders-api", "-n", NAMESPACE, "DB_PASSWORD=wrong"]],
        "restore": [UNDO_API],
    },
    {
        "name": "bad_image_tag",
        "title": "image that does not exist",
        "description": "orders-api is deployed with the tag orders-api:v999",
        "expected": "image_pull_failure",
        "break": [["kubectl", "set", "image", "deployment/orders-api",
                   "orders-api=orders-api:v999", "-n", NAMESPACE]],
        "restore": [UNDO_API],
    },
    {
        "name": "crash_loop",
        "title": "app crashes at startup",
        "description": "orders-api gets DB_PORT=abc, which is not a number",
        "expected": "crash_loop",
        "break": [["kubectl", "set", "env", "deployment/orders-api", "-n", NAMESPACE, "DB_PORT=abc"]],
        "restore": [UNDO_API],
    },
    {
        "name": "out_of_memory",
        "title": "app runs out of memory",
        "description": "orders-api gets a 20Mi memory limit",
        "expected": "out_of_memory",
        "break": [["kubectl", "set", "resources", "deployment/orders-api", "-n", NAMESPACE,
                   "--limits=memory=20Mi"]],
        "restore": [UNDO_API],
    },
]