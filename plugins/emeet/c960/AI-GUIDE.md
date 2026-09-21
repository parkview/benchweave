# Build a BenchWeave device plugin with AI (EMeet C960 4K)

Use one prompt at a time and review its result. This project is a synthetic
scaffold for a UVC webcam. The hardware is the device; this Python package is its
plugin. Use `plugins/<manufacturer>/<name>/` as the project root, with
`src/<package>/` inside; that project can become its own external repository.

## 1. Establish the facts

> Inspect this project and my supplied device evidence (`docs/eMeet-4K.md`: USB
> identity, V4L2 formats, the 16 controls, capture/stream commands). List exact
> model support, intended operations, control ranges/gating, and unknowns. Map
> them to OTDP 0.3.0 and adapter API 1.1. Do not invent controls. Propose a small
> plan before editing. Do not contact hardware or publish.

## 2. Implement against mocks

> Implement the protocol in protocol.py and async adapter.py. Trace every
> control to the V4L2 evidence. Keep create_plugin no-argument, construction/open
> free of device I/O, transport behind the supplied scoped services, mark
> dispatch before transmit, honour deadlines and cancellation, never retry
> silently, preserve uncertain outcomes. Run the synthetic identify/read/write
> tests before replacing them.

## 3. Demonstrate behaviour

> Extend the exact-exchange tests: supported operations, wrong correlation,
> invalid arguments (out-of-range and unknown parameter), expiry/cancellation
> before and after dispatch, malformed/truncated responses, transport loss,
> repeated close. Use SDK validation and conformance helpers. Label synthetic
> evidence separately from device captures. Do not claim hardware qualification.

## 4. Review and qualify separately

> Prepare this exact revision, descriptor, compatibility claims and evidence for
> an independent review using BenchWeave's AI device integration reviewer role.
> Draft a supervised hardware qualification plan (focus sweep, exposure, a real
> still); do not execute it without separate authority.

## 5. Prepare a release for owner review

> Build the wheel and sdist. Prepare the registry manifest, payload
> inventory/hashes, dependency locks, licence, provenance and evidence status.
> A Python wheel is not a registry admission bundle. Show artefacts and remaining
> gaps before publishing.
