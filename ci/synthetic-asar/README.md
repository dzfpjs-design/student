# Synthetic-only ASAR read experiment

This is **not a family-device probe**. The two ASAR fixtures were generated with
`@electron/asar` 3.4.1 using only synthetic text. The parser takes byte arrays,
not arbitrary paths; the harness accepts only the two fixture files by their
fixed SHA-256 values. It returns a module SHA and two fixed marker booleans,
never module or archive content. The GitHub runner fetches only those three
reviewed files from the exact commit and verifies hashes. It requests read-only
contents permission and runs PowerShell 5.1 and 7 as separate normal `-File`
processes; it never changes execution policy or uses encoded commands.

The tests cover expected synthetic bytes, wrong archive hash, truncated input,
invalid header length, invalid module offset and unpacked-module rejection.
They do not establish production-ready handling of arbitrary archives, JSON
duplicate keys, full Unicode or file identity, concurrent mutation, huge input,
security controls on family files, or the version/contents of any real archive.
String-marker presence is **not** proof of a functional credential implementation.
No personal data, device archive, installed application or network service is
read; no credentials are processed. A PowerShell policy denial is a failed test,
not a signal to change or bypass the policy. On each disposable runner the
fixture input files are explicitly removed and the empty stage directory deleted.

The experimental streaming core `readonly-asar.ps1` reads and hashes from held
Windows file handles, bounds header and module sizes, rejects duplicate JSON keys,
and rejects archive files or ancestors that are reparse points. The second
synthetic test exercises locks, hard links and junctions. It is still not a
standalone home-device tool: it accepts caller-selected paths/hashes and has no
approved device-specific entry point. A path must be explicitly pinned before
any production use. Compiling its native helper may create system temporary
files. Pre-existing mapped writes and concurrent hostile changes are not proven
impossible; full hash/identity checks are evidence, not a transactional snapshot.
No family archive or path belongs in this public repository or CI.
