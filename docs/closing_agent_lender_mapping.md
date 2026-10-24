# Closing agent: lender CRM ownership mapping (PROPOSED, preview only)

**Owner decision, 24 Oct 2026: Citizen uses its assigned-agent field only** (labelled "Assignees" in the Citizen
sheet; there is no "Assigned To" column). Created By is not used and no agreement between the two is required.
Across all 1,728 closed Citizen records, Assignees gives: verified agent name 1,118, name not yet verified 526,
blank 65, system account "BFF API" 19. The table below is the earlier field survey.

Owner rule (24 Oct 2026): the lender CRM record is the source of truth for the agent on a closed deal. The RingCX
answering agent stays a separate funnel metric and is never used to pick a closer. The Sales Input HUB / SI tab
is diagnostics only (see `closing_agent_si_investigation.md`). Counts below come from the Oct 22 local exports
(not a live read). Aggregates only.

## Fields per lender (all closed-status records)

| Lender | Field | Closed records | What it holds | Proposed use |
|---|---|---|---|---|
| Citizen | Created By | 1,728 | BFF agent names (upper case); "BFF API" on 168 (system account) | not used (owner decision) |
| Citizen | Assignees | 1,728 | BFF agent names; blank on 65; "BFF API" on 19 | ownership (owner decision) |
| Citizen | Campaign Name | 1,728 | "General" on all | not usable |
| Cordoba | Assigned To | 645 | BFF agent names (title case); "Jacob Aparecio" on 6 (unverified name; see below) | ownership |
| Cordoba | Created By | 645 | "0.0" on 642, "Jacob Aparecio" on 3 | not usable |
| DebtBlue | Assigned To | 50 | blank 19; DebtBlue / Council Law staff on 27; BFF-like names on 4 | ownership only with the company check |
| DebtBlue | Assigned Company | 50 | Debt Blue Servicing 36, Council Law Group 13, Better Future Finance 1 | must be Better Future Finance |
| DebtBlue | Data Source | 50 | blank on all | not usable |

Citizen: Created By and Assignees agree on 1,269 of 1,728 closed records and differ on 459 (149 of those because
Created By is "BFF API", 65 because Assignees is blank, 245 name two different people). Diagnostic only: where
both name different people, the SI credit matches Created By 85 times and Assignees 94 times, so neither field
is reliably "the" closer; where Created By is "BFF API", Assignees matches the SI credit 139 of 142 times.

Cordoba: Assigned To matches the SI credit (diagnostic) on 382 of 473 comparable records; most differences are
spellings of the same person (e.g. "Andrew Patszalek"/"ANDREW PATS", "Camaran Hyder"/"CAM HYDER").

Jacob Aparecio (correction, 24 Oct 2026): he is listed as a user with a BFF email address in the CRM user lists
("Agent ID" tabs) of both the Cordoba and DebtBlue workbooks, so he is no longer classified as a non-BFF value in
`config/report.yaml`. He is not in `name_map`, so his values are an unverified (`unmapped`) name and his records
still show "Closer unknown" until his display name is explicitly approved. He is not a confirmed closer for any
deal.

DebtBlue: Assigned To mostly names lender servicing staff; only the record whose Assigned Company is Better Future
Finance can carry a BFF agent. BFF-like names under other companies ("Arturo Valenzuela", "Austin Bushen",
"Javid Hyder") are not attributed.

## Classification of a closed deal (preview)

The matched lender record from `match_closes` (same phone normalization, closed-status, close-date and lender
precedence rules) supplies the fields. All blank -> `missing`; a system/lender-staff value or a failed company
check -> `non_bff`; two fields naming different people -> `conflicting`; one of two fields blank -> `partial`;
a name without an approved display name -> `unmapped`; otherwise `verified`. Only `verified` shows a name;
everything else is "Closer unknown". `--closer-preview` uses the approved `name_map` (nine exact lender values,
matched ignoring case, plus the approved alias Ben Hall -> Benjamin Hall) and labels its output "approved names".
`proposed_name_map` is kept for reference only and is not used by the preview. Production closer attribution
stays disabled because `source` is null and `lender_values_approved` is false, and `closing_agent` is not
published. The original lender value is kept in the preview for review.
