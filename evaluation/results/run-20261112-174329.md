# Evaluation results

Run started: 2026-11-12 17:43 UTC

Each scenario breaks the lab cluster on purpose, then checks whether the diagnosis finds the expected cause within 150 seconds. The cluster is repaired after each scenario.

**Total: 18 / 18 correct diagnoses**

| Scenario | Runs | Correct | Correct + extra finding | Missed or false alarm | Avg time to diagnose |
|---|---|---|---|---|---|
| nothing broken | 3 | 3 | 0 | 0 | - |
| database turned off | 3 | 3 | 0 | 0 | 10 s |
| wrong database password | 3 | 3 | 0 | 0 | 10 s |
| image that does not exist | 3 | 2 | 1 | 0 | 10 s |
| app crashes at startup | 3 | 3 | 0 | 0 | 10 s |
| app runs out of memory | 3 | 3 | 0 | 0 | 10 s |

## Details

| Scenario | Run | Expected | Judge answered | Result | Time |
|---|---|---|---|---|---|
| nothing broken | 1 | no problem | nothing | CORRECT ✓ | - |
| database turned off | 1 | a dependency scaled to zero | postgres scaled to zero | CORRECT ✓ | 10 s |
| wrong database password | 1 | database login rejected | orders-api cannot log in to postgres | CORRECT ✓ | 10 s |
| image that does not exist | 1 | image cannot be downloaded | image pull failure in orders-api | CORRECT ✓ | 10 s |
| app crashes at startup | 1 | crash loop | orders-api is crash-looping | CORRECT ✓ | 10 s |
| app runs out of memory | 1 | out of memory | orders-api runs out of memory | CORRECT ✓ | 10 s |
| nothing broken | 2 | no problem | nothing | CORRECT ✓ | - |
| database turned off | 2 | a dependency scaled to zero | postgres scaled to zero | CORRECT ✓ | 10 s |
| wrong database password | 2 | database login rejected | orders-api cannot log in to postgres | CORRECT ✓ | 10 s |
| image that does not exist | 2 | image cannot be downloaded | image pull failure in orders-api | CORRECT ✓ | 10 s |
| app crashes at startup | 2 | crash loop | orders-api is crash-looping | CORRECT ✓ | 10 s |
| app runs out of memory | 2 | out of memory | orders-api runs out of memory | CORRECT ✓ | 10 s |
| nothing broken | 3 | no problem | nothing | CORRECT ✓ | - |
| database turned off | 3 | a dependency scaled to zero | postgres scaled to zero | CORRECT ✓ | 10 s |
| wrong database password | 3 | database login rejected | orders-api cannot log in to postgres | CORRECT ✓ | 10 s |
| image that does not exist | 3 | image cannot be downloaded | postgres is rejecting logins; image pull failure in orders-api | CORRECT, but with an extra finding ! | 10 s |
| app crashes at startup | 3 | crash loop | orders-api is crash-looping | CORRECT ✓ | 10 s |
| app runs out of memory | 3 | out of memory | orders-api runs out of memory | CORRECT ✓ | 10 s |
