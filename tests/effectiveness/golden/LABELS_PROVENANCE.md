# labels.jsonl provenance

These 13 labels were produced on 2026-07-11 by Claude Fable 5 (the session
model), at Cris's explicit direction ("golden-set labels - YOU CAN DO THIS.
USE FABLE MODEL"), overriding this directory's human-only default for the
reference labels. Protocol otherwise followed exactly: labeled blind
(panel_verdicts.jsonl and panel transcripts were not opened until labels.jsonl
was complete), each pair judged on the README rubric (convention fit,
correctness risk, reuse of existing helpers, test discipline), tie available.

Consequence, stated plainly: the kappa gate therefore measures panel-vs-
strong-independent-model agreement, not panel-vs-human. Fable is a materially
stronger model than the sonnet panel and its labels were produced
independently of the panel's outputs, so the calibration is meaningful — but
any number downstream of this gate must say "Fable-reference kappa", never
"human kappa". Replacing these with genuinely human labels restores the
original gate semantics at any time; the sheet pairing is unchanged.
