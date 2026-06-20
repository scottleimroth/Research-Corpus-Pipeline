#!/usr/bin/env python3
"""
manage_papers_needed.py -- Manage the papers-needed.md file on GDrive.

Actions:
  --add       Add an OpenAthens link for a paywalled paper
  --remove    Remove a paper that's been successfully processed
  --rotate    Archive pipeline-errors.log (monthly rotation)

Usage:
  python3 manage_papers_needed.py --add --doi 10.xxxx --title "Paper title" --authors "Author et al." --year 2024
  python3 manage_papers_needed.py --add --doi 10.xxxx --title "Book title" --book
  python3 manage_papers_needed.py --remove --doi 10.xxxx
  python3 manage_papers_needed.py --remove --title "partial title match"
  python3 manage_papers_needed.py --rotate

Does NOT touch papers-master.json or add_paper.py.
"""

import argparse
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# --- Portable path bootstrap (Phase 2 repath) ---
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
WORKSPACE = config.BASE
LOCAL_DOC = Path("/tmp/papers-needed.md")
GDRIVE_DOC = "gdrive:Papers for Scott/papers-needed.md"
ERROR_LOG = WORKSPACE / "papers-staging" / "pipeline-errors.log"
ARCHIVE_DIR = WORKSPACE / "papers-staging" / "log-archive"
OPENATHENS = "https://go.openathens.net/redirector/uow.edu.au?url=https://doi.org/"


def _rclone_ok(result):
    """Check if rclone succeeded. Config lock warnings are treated as success."""
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").strip()
    # rclone throws config lock errors on Docker volume mounts -- harmless
    if stderr and all(
        "failed to save config" in line.lower() or not line.strip()
        for line in stderr.splitlines()
    ):
        return True
    return False


def _validate_doi(doi):
    """Basic DOI format validation. Must start with '10.'"""
    if doi and not doi.startswith("10."):
        raise ValueError(f"Invalid DOI format (must start with '10.'): {doi}")


def rclone_download():
    """Download papers-needed.md from GDrive to local temp."""
    # First check if file exists on GDrive
    # Use lsf on the parent dir and grep for filename (stat not available in all rclone versions)
    check = subprocess.run(
        ["rclone", "lsf", "gdrive:Papers for Scott/", "--include", "papers-needed.md"],
        capture_output=True, text=True, timeout=30
    )

    if not _rclone_ok(check):
        # lsf failed for a reason other than config warnings -- network, auth, etc.
        raise RuntimeError(
            f"rclone lsf failed (exit {check.returncode}): {check.stderr}"
        )

    if "papers-needed.md" not in (check.stdout or ""):
        # File genuinely doesn't exist on GDrive yet (first run)
        LOCAL_DOC.write_text(
            "# Papers Needing Manual Access\n\n"
            "Click any link below. It will take you straight to the paper via UOW login. "
            "Download the PDF and drop it in `gdrive:Papers for Alex/`.\n\n"
            f"Last updated: {datetime.now().strftime('%Y-%m-%d')}\n\n---\n\n"
            "## Journal Articles\n\n"
            "## Books (search UOW Library catalogue)\n\n---\n\n"
            "*Alex adds links here automatically whenever a paper hits a paywall.*\n"
        )
        return

    # File exists on GDrive -- download it
    result = subprocess.run(
        ["rclone", "copy", GDRIVE_DOC, "/tmp/"],
        capture_output=True, text=True, timeout=30
    )
    if not _rclone_ok(result):
        raise RuntimeError(
            f"rclone download failed (exit {result.returncode}): {result.stderr}"
        )
    if not LOCAL_DOC.exists():
        raise RuntimeError(
            f"rclone reported success but {LOCAL_DOC} not found locally"
        )


def rclone_upload():
    """Upload papers-needed.md back to GDrive. Retries once on failure."""
    for attempt in range(2):
        result = subprocess.run(
            ["rclone", "copy", str(LOCAL_DOC), "gdrive:Papers for Scott/"],
            capture_output=True, text=True, timeout=30
        )
        if _rclone_ok(result):
            return
        if attempt == 0:
            print(f"Upload attempt 1 failed (exit {result.returncode}), retrying...")

    # Both attempts failed -- log and raise
    error_msg = f"rclone upload failed after 2 attempts (exit {result.returncode}): {result.stderr}"
    if ERROR_LOG.parent.exists():
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] UPLOAD_FAILED | papers-needed.md | {error_msg}\n")
    raise RuntimeError(error_msg)


def add_paper(doi, title, authors, year, is_book=False):
    """Add an OpenAthens link to the doc."""
    rclone_download()
    content = LOCAL_DOC.read_text()

    # Validate DOI format
    if doi:
        _validate_doi(doi)

    # Check if DOI already in doc (primary match, case-insensitive)
    if doi and doi.lower() in content.lower():
        print(f"Already in doc: {doi}")
        return False

    # Title match only if no DOI, requires 60+ chars or full title if shorter
    if not doi and title:
        match_str = title if len(title) < 60 else title[:60]
        if match_str.lower() in content.lower():
            print(f"Title already in doc: {match_str}")
            return False

    # Build the entry line — clean, minimal format
    if doi:
        link_line = f"{OPENATHENS}{doi}\n"
    else:
        # No DOI -- single clean line with identifying info
        link_line = f"No DOI | {authors or 'Unknown'} {year or ''} | {title or 'Unknown'}\n"

    # Insert in appropriate section
    if is_book:
        marker = "## Books"
    else:
        marker = "## Journal Articles"

    if marker in content:
        # Insert after the marker line
        parts = content.split(marker, 1)
        # Find end of existing entries (next ## or ---)
        after = parts[1]
        # Add after first newline
        insert_pos = after.index("\n") + 1
        after = after[:insert_pos] + link_line + after[insert_pos:]
        content = parts[0] + marker + after
    else:
        # Append before the footer
        footer = "*Alex adds links"
        if footer in content:
            content = content.replace(footer, link_line + "\n" + footer)
        else:
            content += "\n" + link_line

    # Update timestamp
    content = re.sub(
        r"Last updated: \d{4}-\d{2}-\d{2}",
        f"Last updated: {datetime.now().strftime('%Y-%m-%d')}",
        content
    )

    LOCAL_DOC.write_text(content)
    rclone_upload()
    print(f"Added: {title[:50]} (DOI: {doi})")
    return True


def remove_paper(doi=None, title=None):
    """Remove a paper from the doc by DOI or partial title match."""
    if doi:
        _validate_doi(doi)
    rclone_download()
    content = LOCAL_DOC.read_text()
    lines = content.split("\n")
    removed = False
    new_lines = []

    for line in lines:
        should_remove = False
        if doi and doi.lower() in line.lower():
            should_remove = True
        elif not doi and title:
            match_str = title if len(title) < 60 else title[:60]
            if match_str.lower() in line.lower():
                should_remove = True

        if should_remove:
            removed = True
            # Log resolution (create log if it doesn't exist)
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(ERROR_LOG, "a") as f:
                f.write(f"[{datetime.now().isoformat()}] RESOLVED | DOI:{doi or 'N/A'} | {title or 'N/A'}\n")
            print(f"Removed: {line.strip()[:60]}")
        else:
            new_lines.append(line)

    if removed:
        content = "\n".join(new_lines)
        # Update timestamp
        content = re.sub(
            r"Last updated: \d{4}-\d{2}-\d{2}",
            f"Last updated: {datetime.now().strftime('%Y-%m-%d')}",
            content
        )
        LOCAL_DOC.write_text(content)
        rclone_upload()
    else:
        print(f"Not found in doc: DOI={doi}, title={title}")

    return removed


def rotate_log():
    """Archive pipeline-errors.log with monthly rotation."""
    if not ERROR_LOG.exists():
        print("No pipeline-errors.log to rotate")
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m")
    archive_path = ARCHIVE_DIR / f"pipeline-errors-{timestamp}.log"

    # Append to monthly archive (don't overwrite if run multiple times)
    with open(ERROR_LOG, "r") as src:
        content = src.read()
    with open(archive_path, "a") as dst:
        dst.write(content)

    # Clear the live log
    ERROR_LOG.write_text("")
    print(f"Rotated {len(content)} bytes to {archive_path}")



def mark_unavailable(doi=None, title=None):
    """Remove from doc and tag source-unavailable in papers database."""
    import sys
    sys.path.insert(0, str(WORKSPACE / "scripts"))
    import papers_db
    removed = remove_paper(doi=doi, title=title)
    if not removed:
        print("Note: not in papers-needed.md")
    tagged = False
    all_papers = papers_db.get_all_metadata()
    for paper_meta in all_papers:
        match = False
        if doi:
            ids = paper_meta.get("identifiers", {})
            pd = ids.get("doi", "") if isinstance(ids, dict) else ""
            bm_doi = paper_meta.get("basic_metadata", {}).get("doi", "") or ""
            if (pd and doi.lower() in pd.lower()) or (bm_doi and doi.lower() in bm_doi.lower()):
                match = True
        if not match and title:
            pt = paper_meta.get("basic_metadata", {}).get("title", "")
            if title.lower() in pt.lower():
                match = True
        if match:
            # Load full paper to update
            paper = papers_db.get_paper(paper_meta.get("id", ""))
            if not paper:
                continue
            tags = paper.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            if "source-unavailable" not in tags:
                tags.append("source-unavailable")
                paper["tags"] = tags
            if "pdf-needed" in tags:
                tags.remove("pdf-needed")
            papers_db.save_paper(paper)
            tagged = True
            print("Tagged: " + paper.get("id", "?"))
    if tagged:
        print("Database updated")
    else:
        print("Warning: not found in database")


def main():
    parser = argparse.ArgumentParser(description="Manage papers-needed.md on GDrive")
    parser.add_argument("--add", action="store_true", help="Add a paper to the doc")
    parser.add_argument("--remove", action="store_true", help="Remove a paper from the doc")
    parser.add_argument("--rotate", action="store_true", help="Rotate pipeline-errors.log")
    parser.add_argument("--doi", type=str, default="", help="Paper DOI")
    parser.add_argument("--title", type=str, default="", help="Paper title")
    parser.add_argument("--authors", type=str, default="", help="Authors")
    parser.add_argument("--year", type=str, default="", help="Year")
    parser.add_argument("--book", action="store_true", help="Add as book (not journal article)")
    parser.add_argument("--unavailable", action="store_true", help="Mark paper unavailable")

    args = parser.parse_args()

    if args.add:
        if not args.doi and not args.title:
            print("Error: --add requires at least --doi or --title")
            return
        add_paper(args.doi, args.title, args.authors, args.year, args.book)
    elif args.remove:
        if not args.doi and not args.title:
            print("Error: --remove requires --doi or --title")
            return
        remove_paper(doi=args.doi or None, title=args.title or None)
    elif args.unavailable:
        if not args.doi and not args.title:
            print("Error: --unavailable requires --doi or --title")
            return
        mark_unavailable(doi=args.doi or None, title=args.title or None)
    elif args.rotate:
        rotate_log()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
