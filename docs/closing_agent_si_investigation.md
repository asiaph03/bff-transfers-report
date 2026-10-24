# Closing agent: SI ledger investigation (read-only, 24 Oct 2026)

Status: **candidate only, not approved.** Nothing here is applied to the dashboard. Aggregates only; no phone
numbers, client names or deal records.

## Where the SI tab comes from

- The SI tab in the Citizen workbook is a single `IMPORTRANGE` of another spreadsheet, "Sales Input HUB V2",
  tab "Sales Input Database", columns A2:AE. SI holds no data of its own; it mirrors the hub.
- The hub is owned by a BFF (betterfuturefinance.com) account, created Dec 2025, and is in active use.
- The hub's other tabs use the same rows for commission work: Paid, Pending, Clawback, Cancelled, NSF,
  AgentNews (per-agent commission status), ImportPayOutMonitoring (with "SI Consolidator", "API Assignee" and
  "SI vs API" columns), and a Dashboard that reconciles "Sales Input Hub" totals per agent against a
  "Consolidated Masterlist", with remarks such as "mismatched agent" and "duplicate phone number".
- No written definition of the columns was found (no header notes, no comments other than one unrelated note).

## Column meanings, from the hub's own formulas

- `Agent` (AA): a stored value per row (not a formula): the agent alias the sale is filed under. Its allowed
  values are the hub's Settings "Agent" list, which matches the System Setup "Agent Alias" list.
- `Cln Agent` (AE): `XLOOKUP(Agent, Settings!M:M, Settings!N:N, Agent)`: Agent passed through the hub's
  "Name Collection" table (e.g. noah -> Noah, Camaran -> Cam, NIck Molina -> Nick, Santi -> Santiago,
  Mudra -> Just Jay, Mclovin -> Handy Andy), otherwise Agent unchanged. It is a cleaned alias of Agent, **not a
  separate role**. All 39 SI rows where Agent differs from Cln Agent are explained by that table.
- `Duplicate Check` (AD): "DupID" when the ID repeats, "DupNumber" when the phone ID repeats.
- Nothing in the hub says whether the Agent is the person who closed (enrolled) the deal, the person paid the
  commission, or both. The commission tabs key on Agent, which suggests the credited sales agent.

## Name tables in the hub (not approved, not applied)

- Settings O:P ("CLN Agent Name" -> "Full Name") and System Setup D/H ("Agent Alias" -> "FULL NAME") give
  full names for most aliases. They disagree in places (spelling and form of the same person, e.g. the "Cam"
  and "Handy Andy" entries), list some aliases more than once, and leave some blank. Row pairing in both tables
  is consistent (each row names the same person on both sides) where both sides are filled.

## Consolidator values (proposed lender mapping, not approved)

Share of SI phones, by Consolidator, enrolled in Citizen's CRM (Oct 22 export): Citizen 86%, CDPCitizens 85%,
IPLFCitizens 88%, Cordoba 3%, Debt Blue 14%, Flux 2%. Share found in Cordoba's CRM for Cordoba rows: 99%; in
DebtBlue's CRM for Debt Blue rows: 98%. The hub's Consolidator list holds CDPCitizens, Citizen, Cordoba and
Debt Blue; Flux, ResolveFlux and IPLFCitizens appear only in older rows.

## The 2027 date

One SI row is dated 2027-05-07. Its MONTH is "May", its expected ACH date is in June 2026, and the rows on
either side in sheet order are dated late April to mid May 2026, so it reads as a year typo for 2026-05-07. It
does **not** match any of the dashboard's closed deals. The preview never uses a ledger row dated after the run
date ("invalid_date") and does not correct it.

## Preview matching (diagnostic only)

Closed deal (existing close rules) -> SI rows with the same normalized phone -> rows whose Consolidator is in
the proposed lender values for the closing lender. 0 rows: `no_phone_match` / `lender_mismatch`; 2+ rows:
`ambiguous` (none is chosen); 1 row: `invalid_date`, `missing`, `conflicting` (Agent differs from Cln Agent),
`unmapped` (no approved full name) or `verified`. Only `verified` names a closer; it never falls back to the
answering agent.
