# BenchWeave

BenchWeave is the project name for the Smart Test Gateway architecture work.

This repository is currently a Python project skeleton plus the frozen architecture and PoC/MVP planning documents. Implementation should begin from the PRD and delivery plan in this folder, with architecture documents treated as contracts rather than informal notes.

## Start Here

- [PoC MVP PRD](implementation-planning/00-poc-mvp-prd.md)
- [Delivery plan](implementation-planning/01-delivery-plan.md)
- [First slice plan](implementation-planning/03-first-slice-plan.md)
- [Architecture v1.5](smart-test-gateway-architecture-v1.5.md)

## Contract Sets

- [OTDP v0.3.0](otdp-v0.3.0/otdp-specification.md)
- [Registry v1.0.0](registry-v1.0.0/registry-specification.md)
- [Execution v1.0.0](execution-v1.0.0/execution-contract.md)
- [Interface v1.1.0](interface-v1.1.0/interface-contract.md)
- [Acceptance closure](acceptance/end-to-end-review.md)


## Web UI

- [Analyse page](analyse-page.md) — browse, re-plot, group and retain captured ADC data.

## Development

Use the [AI device reviewer](ai-device-reviewer.md) to assess candidate integrations against the contracts and their evidence.

Start with the [Device developer guide](device-developer-guide.md) for device creation, gateway hosting and shared packages, including an AI task template and human review checklist.

See [Development and CI](development.md) for uv setup, local checks and GitHub workflows.
