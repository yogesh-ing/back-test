# How to use Settings

**Page:** `/settings` · **Use it when…** you are preparing to trade for real, or
you need to change what a run costs. Saved edits apply to **new** runs — never to
strategies already running.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · design: [MULTI-BROKER-PRD.md](../MULTI-BROKER-PRD.md)

**Header:** `Active broker` selector and `⟳ Refresh`.

## 1. Global live kill-switch

| State | Effect |
|-------|--------|
| `OFF` (default, safe) | Every live run is refused, regardless of segment mode |
| `ON` | Segments with `mode=live`, a positive daily-loss limit **and** a contract-note-validated broker may arm live |

Read the state text before arming anything — this is the master switch.

## 2. Broker cost profiles

Grouped by adoption:

- **In use** — expanded: the active broker plus every broker a segment or the data
  routing points at. *These* are the brokers that price your runs.
- **In `config/brokers.yaml` / Built-in presets** — collapsed behind *Show N other
  broker(s)*: the rate catalogue (10+ presets, so adding a broker is a rates change,
  not a code change). Nothing there is charged until a segment points at it.
- Each card shows provenance (`config/brokers.yaml`, built-in preset, or *this row
  wins* when a panel edit differs from the file), a validation stamp, and
  `Edit / validate`.

**Resolution order is DB row → `config/brokers.yaml` → built-in preset**, so:

1. A broker only in the yaml (e.g. `dhan`) is editable — the first save creates the
   DB row, audited.
2. After that the DB row wins; a later yaml edit does nothing until you change the
   panel row back (the card says so).
3. A contract-note validation stamps the profile, which is also what the live
   arming gate checks.

## 3. Segments

Capital + broker + mode + risk limits per mandate.

1. `+ New segment` opens the editor.
2. Fill the fields; `💾 Save segment (audit-trailed)` records the change.
3. For live segments the **arming checklist** appears inline — work through it
   before expecting `ON` to allow anything.

## 4. Profile editor

`Edit / validate` opens a single broker's rates form. The audit view
(`GET .../<id>/audit`) shows its history; `POST .../<id>/validate` stamps the
contract-note validation the live gate requires.

## Endpoints

`GET/PUT /api/settings/brokers[/<id>]`, `…/<id>/audit`,
`POST …/<id>/validate`, `GET/PUT /api/settings/active-broker`,
`GET/PUT/DELETE /api/settings/segments[/<id>]`, plus the kill-switch endpoints and
the segment live-arming checklist.
