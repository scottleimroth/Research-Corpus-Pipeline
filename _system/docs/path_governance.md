# Path Governance

The corpus uses `pipeline/path_governance.py` as the single policy for filenames
and paths written by active pipeline code. The limits are intentionally below the
Windows 260-character boundary:

- `MAX_FULL_PATH = 240`
- `MAX_FILENAME = 120`
- `MAX_STEM = 100`

Filenames are lowercase ASCII and may contain only letters, digits, underscore,
hyphen, and dot. Names must not contain separators, trailing spaces or dots,
Windows reserved device names such as `CON` or `AUX`, repeated underscore runs,
or unbounded timestamp/retry suffix chains. Extensions are preserved. When a
stem must be shortened, the governance layer preserves meaningful author, year,
and title tokens where available and adds a short stable hash suffix.

## Filename Creation

Active writers should call the governance helpers instead of formatting a path
directly:

- `make_safe_filename(...)` builds canonical source PDF names.
- `reserve_unique_path(...)` applies collision handling with a bounded hash and
  counter strategy.
- `safe_destination_for_file(...)` derives safe staging, review, pending-child,
  cache, and recovery destinations from an existing file.
- `paired_sidecar_paths(...)` and `safe_runtime_rename(...)` keep pending-child
  and staging sidecars paired with their data files.

Long titles belong in JSON metadata, not in runtime filenames.

## DB-Owned Source Renames

Do not manually rename files in `CorpusStore/source-pdfs`. Source PDFs are owned
by `papers.db`; a rename must update the file and DB references together.

Use:

```powershell
python pipeline/audit_path_lengths.py --plan-only --write-report
python pipeline/audit_path_lengths.py --execute --write-report
```

Execute mode first creates a SQLite backup, then renames the file, updates
`file_info.filepath`, `file_info.renamed_filename`, managed path fields, and
matching supplement references in one transaction. If the DB update fails, the
file move is rolled back. Canonical PDFs are never deleted by this utility.

## Staging And Pending Sidecars

Staging files reserve enough filename room for their internal
`.review-retry.json` sidecars in `CorpusStore/staging-metadata`. Pending-child
files reserve enough room for `.meta.json` sidecars in
`pending-parent-child-documents`.

When existing files need repair, `audit_path_lengths.py --execute` renames the
data file and its sidecar together and updates sidecar JSON fields such as
`filename` and `path`.

## Report-Only Areas

Old logs, backups, cache artifacts, audit reports, unknown runtime files, and
code/config files are reported for portability but are not renamed by default.
Their full metadata remains in the path portability report under
`CorpusStore/audit/path-governance/`.

Do not use ad hoc PowerShell renames for DB-owned source PDFs, pending-child
files, or staging files with sidecars. Use the audit utility so ownership,
sidecar pairing, and rename audit records stay intact.
# Path Governance

The pipeline enforces Windows-safe paths before creating, copying, moving, or
persisting managed corpus files. The policy is intentionally below the Windows
260-character legacy limit so the project can be copied, zipped, or moved by
tools that do not opt in to long-path support.

## Limits

- Full path: `240` characters.
- Runtime/source filename: `120` characters.
- Filename stem: `100` characters.
- Safe filename characters: lowercase ASCII letters, digits, underscore,
  hyphen, and dot.
- Windows reserved names (`CON`, `AUX`, `NUL`, `COM1`, `LPT1`, etc.), path
  separators, trailing spaces/dots, repeated underscores, and repeated
  timestamp suffix chains are not allowed.

## Filename Flow

All active file writers should use `pipeline/path_governance.py`:

`identity/title/source name request -> central path policy -> canonical safe filename -> reserved unique destination path -> DB/source transaction or runtime file transaction -> audit record`

Do not build managed corpus filenames directly from raw titles or append
timestamps in loops. Use `make_safe_filename()` for canonical names and
`reserve_unique_path()` or `safe_destination_for_file()` before touching the
filesystem.

## DB-Owned Source PDFs

DB-owned source PDFs in `CorpusStore/source-pdfs` are repaired only by
`pipeline/audit_path_lengths.py --execute`. The repair creates a DB backup,
runs source ownership audit before and after, renames the source file, updates
`file_info.filepath` and `file_info.renamed_filename`, updates applicable
filename/path references in `file_info` and `supplements`, and writes a JSONL
audit record. If any file or DB step fails, the DB transaction is rolled back
and moved files are restored.

Never manually rename canonical source PDFs without updating DB ownership
references in the same transaction.

## Staging And Pending Children

Staging and pending-child repairs use runtime file transactions. Pending child
documents are renamed with their `.meta.json` sidecar. Staging retry sidecars in
`CorpusStore/staging-metadata` move with the staging file. Sidecar JSON may be
updated so it points at the safe filename, while document/PDF bytes remain
unchanged.

## Audit Commands

Plan only:

```bash
python pipeline/audit_path_lengths.py --plan-only --write-report
```

Execute safe repairs:

```bash
python pipeline/audit_path_lengths.py --execute --write-report
```

Reports are written to `CorpusStore/audit/path-governance/`.
