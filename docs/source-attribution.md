# Source attribution

This BRK301 sample combines and adapts the implementations supplied by
Marco Casalaina:

| Source | Imported commit | Destination |
| --- | --- | --- |
| [hotel-agent-a365](https://github.com/mcasalaina/hotel-agent-a365) | `5e0bf01c07fe6af2d9100a8d7e21dda37ec64dc5` | `demos/customer-service` |
| [hotel-bank-finance-agents-365](https://github.com/mcasalaina/hotel-bank-finance-agents-365) | `9b31ab7050f594220df6f532f3324882ff6f0f19` | `demos/payroll-autopilots` |

The source repositories' histories are not imported. The second source may
require access; customers should use this repository's curated implementation,
not obtain private release assets or credentials from the source repository.
The repository's MIT license applies to this sample; dependencies retain their
own licenses. Maintainers should confirm source contribution rights before
public release.

## Adaptations

- Kept the two runtime projects isolated, including original relative imports,
  prompts, deterministic data, tests, and package locks.
- Replaced deployed tenant/resource bindings consistently across examples,
  validators, Fabric definitions, and tests with sample values. Internal
  financial role names and first-party protocol/audience identifiers are retained.
- Replaced personal runtime greetings and seeded tenant mail addresses with
  fictional personas and reserved example addresses.
- Excluded tenant deployment plans, implementation diaries, acceptance reports,
  Foundry cache/results metadata, browser automation, email-cleanup hooks,
  and the private offline dependency archive workflow.
- Added a normal payroll source-build Dockerfile and separate validation jobs.
- Removed the obsolete `test_data_agent_publication.py` suite, which extracted a
  no-longer-present notebook helper and asserted the superseded T-SQL example
  contract. The current native-source publication/readiness tests remain in
  `test_data_agent_runtime_publish.py`; no runtime publication gate was removed.
- Replaced the source landing pages with a customer-facing session narrative and
  setup boundaries. No new cloud deployment or service upgrade is implied.

The optional ASSERT integration retains its separate reviewed upstream source
lock and isolated installation instructions.
