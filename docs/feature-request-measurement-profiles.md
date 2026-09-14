# Feature request — "measurement profiles": named, switchable channel configuration

> Draft for a GitHub issue against `madeinoz67/benchweave`. Written from the
> perspective of an external plugin author (the `adc_6ch_12bit` board plugin).
> Everything under "What BenchWeave has today" is taken from the 0.1.0 / 0.3.0
> contracts; the proposal section is explicitly a proposal, not a description of
> existing behaviour.

## Summary

My ADC plugin lets an operator define, save, and live-switch **measurement
profiles** — named bundles that combine *device settings* (averaging, channel
enablement, sample rate) with *per-channel presentation* (label, unit, gain,
offset, colour, show/hide) and *derived channels* (arithmetic expressions over
raw channels, e.g. `(A0-A1)/.1`). BenchWeave's configuration-preset contract
covers the settings half but has no home for derived channels, per-channel
colour/show, or runtime switching. I'd like to close that gap, ideally by
extending the existing contracts rather than inventing a parallel concept.

## Motivation

The ADC board samples 6 channels and reports raw 12-bit counts. Almost all
real use needs a *view* on top of those counts:

- per-channel gain/offset to convert counts → engineering units (e.g. `V = count
  × 0.001221`),
- a human name and unit per channel (`R21.1`, `V`, `A`),
- derived channels for computed quantities (a capacitor current `(A0-A1)/.1`,
  a scaled rail `A2*2`),
- which channels are shown and in what colour.

Operators flip between these bundles during a session (e.g. a "calibration"
view vs a "characterisation" view). Today all of this lives inside my plugin's
own `config.json` because no upstream contract expresses it.

## What BenchWeave has today

Three distinct places are relevant, and none is a full match:

1. **Configuration presets** (`plugin-ui` 0.1.0, `configuration-preset.schema.json`).
   A preset is a named, versioned, SHA-256-pinned *complete settings document*,
   shipped in `ui/presets/`. It carries identity, revision, compatible
   plugin/profile IDs and firmware versions, a settings-schema ID + digest, the
   complete settings, and `synthetic`/`documented` evidence provenance. Presets
   are static and commissioning-oriented: *"Selecting a preset performs no I/O;
   applying it is a separate operation requiring the normal procedure and
   approval checks."* There is no runtime "save current settings as a preset" or
   "switch preset" flow, and no per-channel colour/show field.

2. **The device descriptor** (OTDP 0.3.0). `channels[]` requires `id`, `label`,
   `role`, `quantities`, `parameter_names` — i.e. a **static** label/unit/role
   per channel, fixed at descriptor time, not a runtime-editable setting.
   `parameters[]` are typed scalars (`float`/`int`/`bool`/`enum`/`string`) with
   `access` (`ro`/`wo`/`rw`), `semantic` (`measurement`/`setpoint`/`state`/
   `configuration`), `unit`, `range`, and a binding. This is the natural home
   for per-channel gain/offset as writable `configuration` parameters, but there
   is no "scaling factor per channel" list — each parameter is one named scalar.

3. **The measurement model** (`otdp-measurement`). Variables carry ID, quantity,
   unit, channel IDs, datatype, dimensions, uncertainty, calibration and quality.
   Calibration is `applied`/`not_applied`/`unknown`. There is **no** derived/
   computed/expression concept anywhere — a variable's values come from the
   device or the adapter, not from an arithmetic expression over other channels.

Note: the word "profile" is already overloaded upstream — `descriptor.profiles`
is a list of device-class profile identifiers, and presets declare *compatible
profile IDs*. A new feature here should pick a distinct name.

## Gap analysis

| My plugin's field | Nearest upstream home | Fit |
|---|---|---|
| `averaging` (device `set_averaging`) | writable `configuration` parameter / configuration action | ✅ |
| channel enablement (`set_channels` mask) | writable parameter or configuration action | ✅ |
| `sample_rate_hz` (host-side decimation) | host acquisition/run setting, not a device property | ✅ (not a preset) |
| per-channel `gain` / `offset` | writable `float` parameters, `semantic: configuration` | ⚠️ possible, not idiomatic (no per-channel list) |
| per-channel `name` | descriptor `channels[].label` | ❌ static only |
| per-channel `unit` | descriptor `channels[].quantities` | ❌ static only |
| derived channels (`(A0-A1)/.1`) | — none — | ❌ gap |
| `show` / `colour` per channel | — none — (presentation/plot bindings only) | ❌ gap |
| `active_profile` live switching | presets are apply-with-approval, no live switch | ❌ lifecycle gap |

## Proposal (scoped, independent options)

These are deliberately separable so a maintainer can accept a subset. I am
happy to implement whichever are welcome.

**A. Ship settings as presets (no new contracts).** Model averaging, channel
enablement, and per-channel gain/offset (as writable `float` parameters) through
the normal configuration-action + settings-schema path, and ship the named
setups as `ui/presets/` documents. Labels/units stay in the descriptor; derived
channels and colours are out of scope. This works today.

**B. Derived channels in the measurement model.** Add an optional "derived
variable" to `otdp-measurement` whose value is computed from other channels by a
safe arithmetic expression (`+ - * / ( )`, numeric literals, channel refs) —
equivalent to a minimal, sandboxed calculator, never arbitrary code. This is the
only piece that is a genuinely *new* concept; everything else can be expressed
with existing fields plus a small parameter-grouping convenience.

**C. Per-channel display hints.** Add an optional per-channel display hint
(colour, hidden) to the presentation layer (`plugin-ui`), kept separate from
device settings so a display choice can never change what the device does.

**D. Runtime profile switching.** Extend preset *application* with a
lease/approval-scoped "switch to this preset" operation for `configuration`-class
settings only, so a profile can be swapped mid-session without re-commissioning —
the current apply-with-approval flow is aimed at commissioning, not interactive
use.

## Questions for the maintainer

1. Is the settings half (Option A) the intended use of configuration presets, or
   are presets meant to stay read-only/commissioning-only?
2. Would you accept a *derived variable* concept in the measurement model
   (Option B), and if so, should the expression be authored in the descriptor,
   in the preset, or in the adapter?
3. Do per-channel colour/show belong in `plugin-ui` (Option C), or are they
   deliberately out of the contract's scope?
4. Is runtime switching (Option D) something you want at all, given the strong
   lease/approval posture of the gateway?

## Repo context

- Plugin: `plugins/adc_6ch_12bit/` (config today: `active_profile`, `profiles`,
  per-channel `name`/`unit`/`gain`/`offset`/`show`/`color`, `computed[]`, and a
  `settings` block).
- Would like to keep the fork independent and contribute this as a focused,
  upstream-aligned change rather than a fork-only feature.
