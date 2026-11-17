# Synthetic recovery gates: local research fixture only

This is **not an installer, migration tool, or safe repair for a family computer**.
It accepts no external paths and creates only generated files in a new Python
`TemporaryDirectory`. It does not read or write actual application, registry,
settings, SQLite, keys, media, or documents. Its registry is a JSON fixture,
and v1/v2 are two synthetic files each. Do not add a production entry point.

Run with Python 3:

```sh
python3 -m unittest discover -s ci/synthetic-recovery-gates -p 'test_*.py' -v
```

Covered locally: exact synthetic file hashes, independently copied and compared
backup and restore including a closed SQLite sample, same-device staging check,
transaction intent before each rename, interruption at four rename boundaries,
synthetic registry snapshot equality, fail-closed checks, and a persisted
no-database-rollback-after-start barrier. The injected interruptions are exceptions,
not power loss or system crash tests. A prelaunch rollback never changes sample data.

Release blockers: no Windows target validation; no actual Windows registry export,
ACLs, attributes, alternate streams, hard-link/reparse-point and junction inventory;
no user authentication/DPAPI recovery check; no active-service exclusion; no
consistent WAL capture (WAL/SHM are explicitly rejected); no production full-file
release manifest or trusted external identity binding; no durable directory flush
or fault-proof concurrent mutation guard; no verified Windows atomic rename, open
handle, antivirus, disk-full, elevation or crash-recovery behavior; no safe treatment
of new learning records after the app starts; no tested rollback of production DB
schema or integration with the true installer, shortcuts, or settings. The simple
manifest records file bytes only and does not inventory empty directories or full
metadata. Backup verification here is fixture evidence, not proof of production
backup completeness or Windows restore. No deployment is authorized by this fixture.
