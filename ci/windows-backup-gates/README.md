# Windows synthetic backup checks

These checks are **not an installer, a production backup utility, or authorization
to touch a family computer**. The Windows job uses a disposable GitHub runner,
its own GUID-named temp directory and HKCU keys, synthetic SQLite records and no
family content, external input paths, project binaries or secrets. A focused
runner-only input is fetched at an exact commit and verified against a reviewed
SHA-256. The normal Windows build is excluded from this isolated branch only.

The planned tests create real HKCU keys, snapshot their values and independently
restore to a separate GUID key; test SQLite's Online Backup API with committed
WAL data and separately restore db/WAL/SHM while the sole synthetic writer is
held by a write transaction; compare readable Owner/Group/DACL SDDL of sample
objects; and reject a synthetic Windows junction by inspecting reparse flags.
The synthetic registry serializer is intentionally limited to its fixed fixture
value types and one child; it is not a generic registry export utility.

No result establishes SACL capture, complete ACL/security descriptor restoration,
Windows DPAPI/key portability, real app quiescence, multiple-DB/media consistency,
production version/hashes, trusted metadata, failure atomicity, power loss, or
non-synthetic device migration. Raw file copying is tested only under known
single-owner synthetic conditions; it is not proof that independently copying a
live application's WAL files is safe. Unknown or untestable conditions block
actual upgrades. The job never executes a home-machine program. Normal execution
on a non-Windows/non-GitHub runner is skipped rather than reported as Windows
validation. Do not enable actual deployment from this test.
