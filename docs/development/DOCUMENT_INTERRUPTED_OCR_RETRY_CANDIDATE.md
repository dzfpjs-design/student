# Document-camera interrupted OCR: isolated candidate

Status: partial; not deployed. Based on public source 8a60866. This does not implement or enable multiframe page reconstruction.

A camera sample may become obsolete while its single-frame OCR is in flight. The existing path correctly refuses to present that result as the current page, but natural hand movement can repeatedly consume the 15-second retry interval. The candidate permits a fresh attempt after at least five seconds when an already-started OCR was invalidated by a visual interruption before revision submission, while the session, child, stream, lease, analysis lifecycle and inspection owner remain current. A fresh retry still requires new decoded frames and the ordinary stability and OCR evidence checks; the previous OCR result is never reused as the current page.

A component allows at most one request starting before the ordinary interval per minute. The budget is counted at the new attempt, retained across device resets within that component, and not persisted across component destruction or application restart. Unknown OCR results do not gain automatic retry authority. This change may increase existing OCR, cloud page-analysis and diagnostic capture calls; actual rate, costs and latency need target-device measurement. Explicit inspection after reconnect may wait for the ordinary interval.

Evidence boundaries:

- code / integration: targeted synthetic frame and component regression only; no complete household or real-child workflow.
- durability: existing persistence paths unchanged; no household data migration or restart acceptance for this candidate.
- target runtime: real Windows, camera, natural writing, lighting and network failures unverified.
- real user / outcome: unverified; no claim of improved learning outcomes.

Unresolved: stable snapshots can still be hard to obtain during continuous writing; slow OCR and persistent occlusion can repeatedly prevent acceptance. The single-frame comparison is not page identity proof. Cross-frame page composition, changing handwriting and corrections, red marks, page turns, children and sessions need a separate evidence and privacy design and target-device tests. Never present a partial candidate as an entire verified page or feed it to mistakes, reports, voice or learning facts without valid provenance.
