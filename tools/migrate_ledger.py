#!/usr/bin/env python3
"""
One-shot migration from the JSON ledgers to ledger.db.

Two independent jobs:

  --downloads   Import download_ledger.json into the `downloads` table.
                Lossless and trivial -- that file is already a flat dict
                keyed on Telegram's permanent file_id.

  --rebuild     Rebuild the `uploads` table by WALKING GOOGLE DRIVE, not by
                reading ledger.json. The old JSON stored paths relative to a
                mutable BASE_DIR global, so a bare "S01E01.mkv" at the tree
                root could have come from any show folder -- the absolute
                paths are genuinely unrecoverable from it. Drive, by
                contrast, holds the real folder structure, so we reconstruct
                from there and get a ledger that reflects reality rather
                than a log of what we believed happened.

  --report      Drift report only. Shows what is in Drive but missing from
                disk and vice versa. Writes nothing. Run this first.

Usage:
    python3 tools/migrate_ledger.py --report
    python3 tools/migrate_ledger.py --rebuild --dry-run
    python3 tools/migrate_ledger.py --all
"""

import os
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import ledger_db
from config import (DIRECTORIES, DRIVE_FOLDERS, DRIVE_FOLDER_LABELS,
                    TARGET_DRIVE_FOLDER_ID)
from gdrive.gdriveup import authenticate

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
OLD_DOWNLOAD_LEDGER = os.path.join(PROJECT_ROOT, "download_ledger.json")
OLD_UPLOAD_LEDGER = os.path.join(PROJECT_ROOT, "ledger.json")

FOLDER_MIME = "application/vnd.google-apps.folder"

# Extensions we treat as media when diffing local dirs against Drive.
MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm",
              ".srt", ".ass", ".sub", ".mka", ".mp3", ".flac", ".m4a"}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------

def build_roots():
    """Map each Drive folder id to the local directories that mirror it.

    One Drive folder can mirror several local roots -- mv and mv2 both point
    at 1#movies but live on different mounts. We record a row for whichever
    local path actually exists on disk (possibly both).
    """
    roots = {}
    for key, fid in DRIVE_FOLDERS.items():
        if not fid:
            continue
        entry = roots.setdefault(fid, {
            "label": DRIVE_FOLDER_LABELS.get(key, key),
            "bases": [],
        })
        base = DIRECTORIES.get(f"/{key}")
        if base and base not in entry["bases"]:
            entry["bases"].append(base)

    if TARGET_DRIVE_FOLDER_ID and TARGET_DRIVE_FOLDER_ID not in roots:
        legacy_bases = [p for p in {DIRECTORIES.get("/docu")} if p]
        roots[TARGET_DRIVE_FOLDER_ID] = {"label": "legacy", "bases": legacy_bases}

    return roots


# ---------------------------------------------------------------------------
# Drive walk
# ---------------------------------------------------------------------------

def list_children(service, folder_id):
    """All non-trashed children of a Drive folder, paginated."""
    items, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageSize=1000,
            pageToken=page_token,
        ).execute(num_retries=5)
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def walk_drive(service, root_id, label, verbose=False):
    """Depth-first walk. Yields (drive_relative_path, parent_id, item)."""
    stack = [(root_id, "")]
    seen_folders = 0
    while stack:
        folder_id, rel_prefix = stack.pop()
        try:
            children = list_children(service, folder_id)
        except Exception as e:
            log(f"   ! failed to list {rel_prefix or '<root>'} ({folder_id}): {e}")
            continue

        seen_folders += 1
        if verbose and seen_folders % 25 == 0:
            log(f"   ... {seen_folders} folders scanned in {label}")

        for item in children:
            rel = os.path.join(rel_prefix, item["name"]) if rel_prefix else item["name"]
            yield rel, folder_id, item
            if item.get("mimeType") == FOLDER_MIME:
                stack.append((item["id"], rel))


# ---------------------------------------------------------------------------
# Job: rebuild uploads from Drive
# ---------------------------------------------------------------------------

def rebuild(dry_run=False, include_missing=False, verbose=False):
    roots = build_roots()
    if not roots:
        log("No Drive folders configured. Check MOVIES_DRIVE_FOLDER_ID / "
            "TV_DRIVE_FOLDER_ID in .env")
        return

    log("Authenticating with Google Drive...")
    service = authenticate()

    rows, missing, total_seen = [], [], 0

    for fid, meta in roots.items():
        label, bases = meta["label"], meta["bases"]
        if not bases:
            log(f"\n[{label}] ({fid}) -- no local base dir configured, skipping.")
            continue

        log(f"\n[{label}] ({fid})")
        log(f"   local roots: {', '.join(bases)}")

        count = 0
        for rel, parent_id, item in walk_drive(service, fid, label, verbose):
            total_seen += 1
            is_folder = item.get("mimeType") == FOLDER_MIME

            matched = False
            for base in bases:
                local_path = os.path.join(base, rel)
                if os.path.exists(local_path):
                    rows.append({
                        "path": local_path,
                        "parent_id": parent_id,
                        "gid": item["id"],
                        "name": item["name"],
                        "is_folder": is_folder,
                        "size_bytes": int(item["size"]) if item.get("size") else None,
                        "uploaded_at": item.get("modifiedTime") or None,
                        "source": "rebuild",
                    })
                    matched = True
                    count += 1

            if not matched:
                missing.append((label, rel))
                if include_missing:
                    local_path = os.path.join(bases[0], rel)
                    rows.append({
                        "path": local_path,
                        "parent_id": parent_id,
                        "gid": item["id"],
                        "name": item["name"],
                        "is_folder": is_folder,
                        "size_bytes": int(item["size"]) if item.get("size") else None,
                        "uploaded_at": item.get("modifiedTime") or None,
                        "source": "rebuild-nodisk",
                    })
                    count += 1

        log(f"   matched {count} item(s)")

    log(f"\nDrive items seen:      {total_seen}")
    log(f"Rows to write:         {len(rows)}")
    log(f"In Drive, not on disk: {len(missing)}")

    if missing:
        log("\n  (first 20 Drive items with no local counterpart --")
        log("   normal if you have been using `/mv gd del`)")
        for label, rel in missing[:20]:
            log(f"    [{label}] {rel}")
        if len(missing) > 20:
            log(f"    ...and {len(missing) - 20} more")

    if dry_run:
        log("\n--dry-run: nothing written.")
        return

    written = ledger_db.put_uploads_bulk(rows)
    log(f"\nWrote {written} rows into {ledger_db.DB_PATH}")


# ---------------------------------------------------------------------------
# Job: import download_ledger.json
# ---------------------------------------------------------------------------

def import_downloads(dry_run=False):
    if not os.path.exists(OLD_DOWNLOAD_LEDGER):
        log(f"No {OLD_DOWNLOAD_LEDGER} found -- nothing to import.")
        return

    with open(OLD_DOWNLOAD_LEDGER, "r") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        log("download_ledger.json is not a dict -- refusing to import.")
        return

    log(f"Found {len(data)} download record(s).")
    if dry_run:
        log("--dry-run: nothing written.")
        return

    for file_uid, entry in data.items():
        if isinstance(entry, dict):
            ledger_db.put_download(file_uid, entry)

    log(f"Imported {len(data)} download record(s).")


# ---------------------------------------------------------------------------
# Job: drift report (read-only)
# ---------------------------------------------------------------------------

def report():
    roots = build_roots()
    log("Authenticating with Google Drive...")
    service = authenticate()

    for fid, meta in roots.items():
        label, bases = meta["label"], meta["bases"]
        if not bases:
            continue

        log(f"\n=== {label} ({fid}) ===")

        drive_rel = set()
        for rel, _parent, item in walk_drive(service, fid, label):
            if item.get("mimeType") != FOLDER_MIME:
                drive_rel.add(rel)

        for base in bases:
            if not os.path.isdir(base):
                log(f"  local root missing: {base}")
                continue

            local_rel = set()
            for dirpath, _dirs, files in os.walk(base):
                for fname in files:
                    if os.path.splitext(fname)[1].lower() not in MEDIA_EXTS:
                        continue
                    full = os.path.join(dirpath, fname)
                    local_rel.add(os.path.relpath(full, base))

            only_local = sorted(local_rel - drive_rel)
            only_drive = sorted(drive_rel - local_rel)

            log(f"\n  {base}")
            log(f"    on disk:            {len(local_rel)}")
            log(f"    in Drive:           {len(drive_rel)}")
            log(f"    on disk, NOT Drive: {len(only_local)}")
            log(f"    in Drive, NOT disk: {len(only_drive)}")

            for rel in only_local[:15]:
                log(f"      [not uploaded] {rel}")
            if len(only_local) > 15:
                log(f"      ...and {len(only_local) - 15} more")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Migrate JSON ledgers to SQLite.")
    ap.add_argument("--downloads", action="store_true",
                    help="import download_ledger.json into the downloads table")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the uploads table by walking Google Drive")
    ap.add_argument("--report", action="store_true",
                    help="read-only drift report; writes nothing")
    ap.add_argument("--all", action="store_true",
                    help="equivalent to --downloads --rebuild")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen, write nothing")
    ap.add_argument("--include-missing", action="store_true",
                    help="also record Drive items that have no local file")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--stats", action="store_true",
                    help="print ledger.db stats and exit")
    args = ap.parse_args()

    if args.stats:
        import pprint
        pprint.pprint(ledger_db.stats())
        return

    if not any([args.downloads, args.rebuild, args.report, args.all]):
        ap.print_help()
        return

    started = datetime.now()

    if args.report:
        report()
        return

    if args.downloads or args.all:
        log("\n########## IMPORT download_ledger.json ##########")
        import_downloads(dry_run=args.dry_run)

    if args.rebuild or args.all:
        log("\n########## REBUILD uploads FROM DRIVE ##########")
        rebuild(dry_run=args.dry_run,
                include_missing=args.include_missing,
                verbose=args.verbose)

    if not args.dry_run:
        log("\n########## RESULT ##########")
        import pprint
        pprint.pprint(ledger_db.stats())

    log(f"\nDone in {(datetime.now() - started).total_seconds():.1f}s")


if __name__ == "__main__":
    main()
