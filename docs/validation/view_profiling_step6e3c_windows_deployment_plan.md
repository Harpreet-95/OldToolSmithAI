# Phase 7, Step 6E3C — Windows-safe deployment & rollback plan (draft, not executed)

Supersedes any earlier Linux-shaped draft for this step. Every command below is PowerShell 5.1-compatible, uses absolute paths, and never assumes `sqlite3`/`sha256sum`/`$(date)` are on `PATH`. This plan is **not executed** as part of Step 6E3B3/6E3B4 — it is a draft for review before Step 6E3C actually runs.

**Updated in Step 6E3B4** (code-rollback section added below, Step 2d/5) to cover the additional uncommitted code this step adds on top of 6E3B3: `core/sqlservice/view_profiler.py` (capture-status-aware truncation override), `core/sqlservice/sql_validation.py` (unchanged from 6E3B3), `data/view_profile_service.py`, `data/view_semantics_service.py` (the MSSQL bulk complete-definition capture fix), `data/investigation_service.py` (the truncation-constant fix), `data/models.py` (two new additive, nullable `view_semantic_evidence` columns — see Step 6E3B4's own report for why this is additive-only, never a destructive schema change), plus new/updated tests and `docs/validation/*` artifacts.

This draft intentionally reuses the backup mechanics `data/migrate_data_dir.py` already implements and ships in this repo (WAL checkpoint → `shutil.copy2` → byte-size verification) rather than inventing a new mechanism. It adds a checksum step on top (`Get-FileHash`) since `migrate_data_dir.py`'s own size-only check is a weaker guarantee than Step 6E3C should rely on for a rollback-critical backup.

## What this plan must never do

- No `git checkout`, `git reset`, `git stash`, or any other command that discards uncommitted work. The branch `feature/ccpp-metadata-intelligence` currently carries a very large amount of uncommitted/untracked work (confirmed via `git status` at the start of this step) — any destructive git command here would be catastrophic and is explicitly out of bounds.
- No shell-dependent `$(date)` — PowerShell uses `Get-Date -Format`.
- No assumption that `sqlite3.exe` or `sha256sum` is installed — this plan uses only Python (already a hard dependency of this project) and PowerShell's built-in `Get-FileHash`.

## Step 1 — Stop the application

```powershell
# Identify and stop whatever is currently running the app (adjust to the actual process name/port in use).
Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*ToolSmithAI*" }
# Stop it the same way it was started (Ctrl+C in its terminal, or):
# Stop-Process -Id <pid> -Confirm:$false
```

A running process can still be writing to `toolsmith.db` in WAL mode — a copy taken mid-write is not guaranteed consistent even with the checkpoint step below. Confirm the process has actually exited before continuing.

## Step 2 — Take a verified backup (reuses `data/migrate_data_dir.py`)

```powershell
$RepoRoot   = (Get-Location).Path  # run from the cloned repository root
$Timestamp  = Get-Date -Format "yyyyMMddTHHmmssZ"
$BackupDir  = Join-Path $RepoRoot "data\backups\pre_step6e3c_$Timestamp"

Set-Location $RepoRoot
& .\venv\Scripts\python.exe -m data.migrate_data_dir --dest $BackupDir
```

This runs the repo's own `migrate()` function: it WAL-checkpoints `data/toolsmith.db`, then `shutil.copy2`s the DB file, its `-wal`/`-shm` siblings (if present), the audit log, and every rotated `.bak` audit log into `$BackupDir`, verifying each copy's byte size against its source and raising if they disagree. Originals are **copied, never moved** — nothing at the source is touched.

### Step 2b — Add a checksum on top (stronger than the script's own size check)

```powershell
$LiveDb    = Join-Path $RepoRoot "data\toolsmith.db"
$BackupDb  = Join-Path $BackupDir "toolsmith.db"

$LiveHash   = Get-FileHash -Path $LiveDb   -Algorithm SHA256
$BackupHash = Get-FileHash -Path $BackupDb -Algorithm SHA256

if ($LiveHash.Hash -ne $BackupHash.Hash) {
    Write-Error "Backup checksum mismatch — DO NOT PROCEED. Live: $($LiveHash.Hash) Backup: $($BackupHash.Hash)"
} else {
    Write-Host "Backup verified: $($BackupHash.Hash)"
    $BackupHash.Hash | Out-File -FilePath (Join-Path $BackupDir "toolsmith.db.sha256") -Encoding utf8
}
```

Do not proceed to Step 3 unless this prints "Backup verified".

### Step 2c — Save a config snapshot

```powershell
Copy-Item (Join-Path $RepoRoot ".env") (Join-Path $BackupDir ".env.snapshot") -ErrorAction SilentlyContinue
git -C $RepoRoot rev-parse HEAD | Out-File -FilePath (Join-Path $BackupDir "git_head.txt") -Encoding utf8
git -C $RepoRoot status --short | Out-File -FilePath (Join-Path $BackupDir "git_status_at_backup.txt") -Encoding utf8
```

`git rev-parse`/`git status` here are **read-only** — they record state, they never modify the working tree.

### Step 2d — Snapshot the CODE changes (per-file checksums, never a destructive Git operation)

Phase 7, Step 6E3B4, Part 10's own explicit requirement: code rollback must be based on recorded per-file checksums/patches or a recoverable workspace snapshot — **never** `git checkout`/`git reset`/`git stash`, which would discard whatever other uncommitted work is sitting in this branch alongside this step's own changes (confirmed large and pre-existing — see `git_status_at_backup.txt` from Step 2c).

```powershell
$RepoRoot    = (Get-Location).Path  # run from the cloned repository root
$BackupDir   = Join-Path $RepoRoot "data\backups\pre_step6e3c_<TIMESTAMP>"  # same dir as Step 2
$CodeSnapDir = Join-Path $BackupDir "code_snapshot"
New-Item -ItemType Directory -Path $CodeSnapDir -Force | Out-Null

# Every file this engagement (Step 6E3B3 + 6E3B4) touched or added. Extend
# this list if Step 6E3C itself changes further files before backfill.
$ChangedFiles = @(
    "core\sqlservice\view_profiler.py",
    "core\sqlservice\sql_validation.py",
    "data\view_profile_service.py",
    "data\view_semantics_service.py",
    "data\investigation_service.py",
    "data\models.py",
    "tests\test_sqlservice_view_profiler.py",
    "tests\test_sql_validation_secrets_and_suitability.py",
    "tests\test_view_semantics_complete_definition_capture.py"
)

$Manifest = @()
foreach ($rel in $ChangedFiles) {
    $full = Join-Path $RepoRoot $rel
    if (Test-Path $full) {
        $destRel = $rel -replace '\\', '__'
        Copy-Item -Path $full -Destination (Join-Path $CodeSnapDir $destRel) -Force
        $hash = (Get-FileHash -Path $full -Algorithm SHA256).Hash
        $Manifest += [PSCustomObject]@{ Path = $rel; Sha256 = $hash; SnapshotFile = $destRel }
    } else {
        Write-Warning "Expected file not found, skipping: $rel"
    }
}
$Manifest | ConvertTo-Json | Out-File -FilePath (Join-Path $CodeSnapDir "manifest.json") -Encoding utf8
Write-Host "Code snapshot written: $CodeSnapDir ($($Manifest.Count) files, manifest.json has per-file SHA-256)"
```

This is a **whole-file copy + checksum manifest**, not a Git object — restoring from it (Step 5 below) is a plain file copy, so it can never touch, stash, or discard any OTHER uncommitted file in the working tree.

## Step 3 — Apply the actual Step 6E3C change (not specified by this draft)

Whatever Step 6E3C's own deployment action turns out to be (a migration, a connect-time re-profiling run, a config flag flip) is intentionally not detailed here — this draft only covers the backup/rollback scaffolding that step will need, per this milestone's own Part 10/11 scope boundary ("do not execute deployment or rollback in this step").

## Step 4 — Restore procedure (if Step 6E3C needs to be rolled back)

```powershell
$RepoRoot  = (Get-Location).Path  # run from the cloned repository root
$BackupDir = Join-Path $RepoRoot "data\backups\pre_step6e3c_<TIMESTAMP>"  # fill in the exact directory from Step 2
$LiveDb    = Join-Path $RepoRoot "data\toolsmith.db"
$BackupDb  = Join-Path $BackupDir "toolsmith.db"

# 1) Stop the app again (see Step 1) -- never restore under a live writer.

# 2) Re-verify the backup's own integrity BEFORE touching the live file.
$StoredHash  = Get-Content (Join-Path $BackupDir "toolsmith.db.sha256")
$CurrentHash = (Get-FileHash -Path $BackupDb -Algorithm SHA256).Hash
if ($StoredHash -ne $CurrentHash) {
    Write-Error "Backup file has changed since it was taken -- DO NOT RESTORE FROM IT. Investigate first."
} else {
    # 3) Move the CURRENT (post-6E3C) live db aside instead of deleting it --
    #    reversible in case the restore itself needs to be undone.
    $QuarantineTimestamp = Get-Date -Format "yyyyMMddTHHmmssZ"
    $QuarantineDir = Join-Path $RepoRoot "data\backups\quarantined_live_$QuarantineTimestamp"
    New-Item -ItemType Directory -Path $QuarantineDir -Force | Out-Null
    foreach ($suffix in "", "-wal", "-shm") {
        $f = "$LiveDb$suffix"
        if (Test-Path $f) {
            Move-Item -Path $f -Destination (Join-Path $QuarantineDir (Split-Path $f -Leaf)) -Force
        }
    }

    # 4) Copy the verified backup into place.
    Copy-Item -Path $BackupDb -Destination $LiveDb -Force
    $RestoredHash = (Get-FileHash -Path $LiveDb -Algorithm SHA256).Hash
    if ($RestoredHash -ne $StoredHash) {
        Write-Error "Restored file checksum does not match backup -- investigate before restarting the app."
    } else {
        Write-Host "Restore verified ($RestoredHash). Quarantined post-change DB kept at $QuarantineDir for forensic comparison -- delete it yourself only after confirming the rollback is correct."
    }
}

# 5) Restart the app and verify it reads correctly from the restored DB before declaring the rollback complete.
```

Nothing in this restore path uses `git checkout`/`reset`/`stash`, deletes the post-change database outright (it is quarantined, not removed), or assumes any tool beyond Python and PowerShell's own built-ins.

## Step 5 — Restore CODE from the per-file snapshot (if Step 6E3C's own code change needs rolling back)

```powershell
$RepoRoot    = (Get-Location).Path  # run from the cloned repository root
$BackupDir   = Join-Path $RepoRoot "data\backups\pre_step6e3c_<TIMESTAMP>"  # same dir as Step 2d
$CodeSnapDir = Join-Path $BackupDir "code_snapshot"
$Manifest    = Get-Content (Join-Path $CodeSnapDir "manifest.json") | ConvertFrom-Json

foreach ($entry in $Manifest) {
    $target = Join-Path $RepoRoot $entry.Path
    $source = Join-Path $CodeSnapDir $entry.SnapshotFile

    # Quarantine the CURRENT (post-6E3C) version first -- never overwritten
    # in place, so this step is itself reversible.
    if (Test-Path $target) {
        $QuarantineTimestamp = Get-Date -Format "yyyyMMddTHHmmssZ"
        $QuarantineDir = Join-Path $BackupDir "quarantined_code_$QuarantineTimestamp"
        New-Item -ItemType Directory -Path $QuarantineDir -Force | Out-Null
        Copy-Item -Path $target -Destination (Join-Path $QuarantineDir ($entry.Path -replace '\\', '__')) -Force
    }

    Copy-Item -Path $source -Destination $target -Force
    $RestoredHash = (Get-FileHash -Path $target -Algorithm SHA256).Hash
    if ($RestoredHash -ne $entry.Sha256) {
        Write-Error "Checksum mismatch restoring $($entry.Path) -- investigate before proceeding."
    } else {
        Write-Host "Restored $($entry.Path) (sha256=$RestoredHash, verified against manifest)"
    }
}
# Any file NOT in $Manifest (anything else uncommitted in the working tree,
# including work unrelated to this engagement) is NEVER touched by this
# step -- a plain per-file copy restore, by construction, cannot reach
# outside the list it was given.
```

Every other uncommitted file in the working tree — including the large amount of pre-existing, unrelated uncommitted work already on this branch — is left completely untouched by both Step 2d and Step 5, because both operate on an explicit, named file list, never a repo-wide or Git-level operation.

## Notes

- This plan was drafted read-only: no command in it has been executed as part of Step 6E3B3. `data/migrate_data_dir.py` itself was only **read**, never modified or invoked for real, during this step.
- `$BackupDir`/`$QuarantineDir` naming (`pre_step6e3c_<timestamp>`, `quarantined_live_<timestamp>`) matches the existing ad-hoc convention already visible in `data/backups/` (e.g. `toolsmith_pre_step3j_20260927T164728Z.db`).
