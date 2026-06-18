# PNR Event-History (Phase 2) — Step-0 discovery findings

Captured from a live, authenticated probe (`tools/probe_pnr_history.py`) on **2026-06-18**
against one real dossier (PNR `09AHEA`, dossier `15605650`). No PII is recorded here; the raw
captures live under `tests/fixtures/pnr_history/` which is **git-ignored** (they contain a real
passenger phone, BKASH transaction id, and agency name — never commit them).

## Confirmed endpoints

`dossier_id` comes free from `zenith_pnr_client.lookup_pnr(sess, pnr).dossier_id`.
Auth is the existing session **cookie** — there is **no token in the URL**, so the app's
authenticated `ZenithSession` can call these directly.

| Tab | Request |
|---|---|
| **Changes history** (events log — the Phase-2 gap signals) | `GET /newui/aerien/commun/search_event.asp?contexte=recap_dossier&CategorieEvent=3&id_dossier_vol=<dossier_id>` |
| → clean columnar view | append **`&excel=1`** (returns a parseable 8-column table) |
| **Ticket history** (issue/reissue/refund/void detail) | `GET /newui/aerien/recettesco/HistoBillet.asp?haction=SEARCH&id_Dossier=<dossier_id>` |

> **Critical param gotcha:** the changes endpoint takes **`id_dossier_vol`**, NOT `id_dossier`.
> Passing `id_dossier` returns Zenith's generic "bad data" HTTP 500. (`HistoBillet` uses `id_Dossier`.)

### `CategorieEvent` behaviour (empirical)
- `CategorieEvent=1` and `=3` returned the **identical full log** (~120 KB) — the comment-bearing
  "File Modification" events. **Use `CategorieEvent=3`** (matches the browser).
- `=2,4,5,6,7,8` returned a smaller (~35 KB) flight-time-change subset. Not needed.
- So: **one request per dossier** (`cat3` + `excel=1`) covers the changes log.

### `excel=1` works for the dossier context
This was an open risk in the plan ("&excel=1 for the dossier context may not work"). **Resolved —
it works** and yields clean columns, so we parse the `excel=1` view (not the messy HTML view).

## Changes-history `excel=1` table shape

Header: `Date | Created by | Description | Type | PNR | Customer | Flight | Passenger`

- **Date**: `14/06/2026 07:50` (GMT in the excel view; the HTML view shows local + GMT).
- **Created by**: `Chakrabarty Taposh (taposh2589/DAC-16 Banani New)` → display name + `user_id` + office.
  Reuse `zenith_history_parser.parse_agent` + a `_split_office`.
- **Description**: free text; multiple facts joined by literal `<br>`. Split on `<br>` then classify each.

### Event vocabulary seen (Type / Description)
| Pattern | Meaning | Detector relevance |
|---|---|---|
| `Comment: PAX CONTACT-<old> -> PAX CONTACT-<new>` | contact set/changed | **contact churn** |
| `Comment: <method> PAYMENT//Transaction ID-<txn>//` | payment captured | **payment-txn reuse** |
| `Issued<br>IATA Coupon status :->I` | coupon issued | issue |
| `Issued->Exchanged<br>IATA Coupon status :I ->E` | **REISSUE** (exchange) | **explicit reissue** (Phase 1 could only infer this) |
| `Issued->Airport control … :I ->AL` | gate/airport control | flown-ish |
| `Issued->Checked` | checked in | flown-ish |
| `Issued<br>Baggs/Weights : 0/0 -> 1/25 … Numbers : -> 3779884553` | EMD/baggage add | ancillary |
| `Changing flight time` | schedule change | involuntary context |
| `Cancellation modifications in progress : flight BS361 …` | schedule cancel | involuntary context |
| `void synchronization …` | void | void |

### Proposed extraction regexes (validate against more samples before shipping)
```
CONTACT  = r"PAX CONTACT-(?P<old>\S*)\s*->\s*PAX CONTACT-(?P<new>\S+)"
PAYMENT  = r"(?P<method>[A-Z][A-Z ]*?)\s*PAYMENT//Transaction ID-(?P<txn>[A-Za-z0-9]+)//"
REISSUE  = r"IATA Coupon status\s*:\s*I\s*->\s*E"          # or Type contains "Exchanged"
COUPON   = r"IATA Coupon status\s*:\s*(?P<from>\w+)\s*->\s*(?P<to>\w+)"
```
Note: in `09AHEA`, `PAX CONTACT old == new` (a re-save, not a real change) — the contact-churn
detector must compare old≠new, not merely count `PAX CONTACT` mentions.

## Ticket history (`HistoBillet.asp`) — NOT yet mapped
Returned HTTP 200 (~37 KB) but a **different table layout** (`_TableReader` saw `Ticket log` /
`Issuing date` / `Transaction` as a label-value structure, not the flat 8-col grid). `&excel=1`
returned the same bytes (no effect). **TODO:** read the raw HTML and map its real row structure
before relying on it. The changes-history `cat3` already covers reissue (via `I->E`), so ticket
history is a secondary corroboration source, not a blocker.

## Operational notes
- **Zenith is intermittently 504-ing** (CloudFront). The probe now retries 5× w/ backoff; `lookup_pnr`
  retries 3×. One of two probed PNRs (`08EJOJ`) still 504'd through all retries. Run off-peak.
- Per dossier we need **2 GETs** (changes `cat3&excel=1`, ticket history) — far cheaper than the
  8-category sweep the probe does for discovery.

## What's still needed before building Phase 2 detectors
1. **More real samples** — especially PNRs with an actual **reissue / refund / void**, a **real
   contact change** (old≠new), and **multiple payments**, so the cross-PNR detectors
   (payment-txn reuse, contact churn) can be built *and tested* against misuse-shaped data.
   (`09AHEA` is a clean/normal booking — good for format, not for detector validation.)
2. **Map `HistoBillet`'s table** (read the saved raw HTML).
3. Then: `zenith_pnr_history_downloader` (2 GETs/dossier, governor + budget, per-dossier checkpoint)
   → parser (above regexes) → payment-txn-reuse + contact-churn detectors folded into the existing
   `run_pnr_misuse_audit` → GUI sub-tab. Tests use inline synthetic HTML (repo convention; the
   `tests/fixtures/` dir is git-ignored).
