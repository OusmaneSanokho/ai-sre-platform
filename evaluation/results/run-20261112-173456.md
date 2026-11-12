# Evaluation results

Run started: 2026-11-12 17:34 UTC

Each scenario breaks the lab cluster on purpose, then checks whether the diagnosis finds the expected cause within 150 seconds. The cluster is repaired after each scenario.

**Total: 6 / 6 correct diagnoses**

| Scenario | Runs | Correct | Correct + extra finding | Missed or false alarm | Avg time to diagnose |
|---|---|---|---|---|---|
| nothing broken | 1 | 1 | 0 | 0 | - |
| database turned off | 1 | 1 | 0 | 0 | 10 s |
| wrong database password | 1 | 1 | 0 | 0 | 10 s |
| image that does not exist | 1 | 1 | 0 | 0 | 10 s |
| app crashes at startup | 1 | 1 | 0 | 0 | 10 s |
| app runs out of memory | 1 | 1 | 0 | 0 | 10 s |

## Details

| Scenario | Run | Expected | Judge answered | Result | Time |
|---|---|---|---|---|---|
| nothing broken | 1 | no problem | nothing | CORRECT ✓ | - |
| database turned off | 1 | a dependency scaled to zero | postgres scaled to zero | CORRECT ✓ | 10 s |
| wrong database password | 1 | database login rejected | orders-api cannot log in to postgres | CORRECT ✓ | 10 s |
| image that does not exist | 1 | image cannot be downloaded | image pull failure in orders-api | CORRECT ✓ | 10 s |
| app crashes at startup | 1 | crash loop | orders-api is crash-looping | CORRECT ✓ | 10 s |
| app runs out of memory | 1 | out of memory | orders-api runs out of memory | CORRECT ✓ | 10 s |
