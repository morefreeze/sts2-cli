#!/usr/bin/env python3
"""extract_pck.py — Extract files from a Godot 4.x .pck pack archive.

The Slay the Spire 2 game ships its art / scenes / audio inside a 1.7 GB
PCK file at:
    .../Slay the Spire 2.app/Contents/Resources/Slay the Spire 2.pck

This script reads the PCK header, walks the file directory, and either
LISTS matching paths (default) or EXTRACTS them to a destination dir.
Use a path-pattern filter (substring or glob-like) to keep the extraction
manageable — the full PCK has ~5k+ files.

PCK format reference (format version 3, Godot 4.x):

    Magic "GDPC" (4) | pack_format=3 (4) | major (4) | minor (4) | patch (4)
    | pack_flags (4) | files_base (8) | reserved 16*u32 (64) | file_count (4)
    [ per file:
        path_len (4) | path (path_len bytes, NUL-padded to 4-byte aligned)
        offset (8) | size (8) | md5 (16) | file_flags (4)  # flags only if pack_flags & 1
    ]
    [ file data ]

Usage:
    .venv/bin/python agent/extract_pck.py LIST                  # all paths
    .venv/bin/python agent/extract_pck.py LIST --grep ironclad  # match substring
    .venv/bin/python agent/extract_pck.py EXTRACT --grep ironclad --out data/sts2_assets/
"""
import argparse
import fnmatch
import os
import struct
import sys
from pathlib import Path

DEFAULT_PCK = os.path.expanduser(
    "~/Library/Application Support/Steam/steamapps/common/"
    "Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/Slay the Spire 2.pck"
)


def read_header(f) -> dict:
    """Read PCK header. For format 3 the file directory lives at the END of the
    pack and is referenced by an additional u64 pck_dir_offset after files_base
    (rather than inline after a small reserved area). For formats 1/2 the
    directory follows the header inline.

    Returns dict with file_count, files_base, and a ready-positioned file
    handle (seek already at the first directory entry)."""
    raw = f.read(4)
    if raw != b"GDPC":
        raise ValueError(f"Not a Godot PCK file (magic was {raw!r})")
    pack_format = struct.unpack("<I", f.read(4))[0]
    if pack_format not in (1, 2, 3):
        raise ValueError(f"Unsupported PCK format version: {pack_format}")
    major = struct.unpack("<I", f.read(4))[0]
    minor = struct.unpack("<I", f.read(4))[0]
    patch = struct.unpack("<I", f.read(4))[0]
    pack_flags = 0
    files_base = 0
    pck_dir_offset = 0
    if pack_format >= 2:
        pack_flags = struct.unpack("<I", f.read(4))[0]
        files_base = struct.unpack("<Q", f.read(8))[0]
    if pack_format >= 3:
        pck_dir_offset = struct.unpack("<Q", f.read(8))[0]
        # Reserved 16*u32 follows but we don't need it; the directory is at
        # pck_dir_offset (typically near end of file).
        f.seek(pck_dir_offset)
    else:
        # 16 u32 reserved still appear inline before file_count
        f.read(16 * 4)
    file_count = struct.unpack("<I", f.read(4))[0]
    if file_count > 10_000_000:
        raise ValueError(f"Implausible file_count={file_count} — header decode wrong")
    return {
        "pack_format": pack_format,
        "godot_version": f"{major}.{minor}.{patch}",
        "pack_flags": pack_flags,
        "files_base": files_base,
        "pck_dir_offset": pck_dir_offset,
        "file_count": file_count,
        "dir_start": f.tell(),
    }


def walk_directory(f, header: dict):
    """Iterate the file directory, yielding {path, offset, size, flags}.

    For format >= 2 each entry has per-file flags after the md5 (4 bytes),
    even when pack_flags has no encryption bit set. Format 1 omits them."""
    has_per_file_flags = header["pack_format"] >= 2
    for _ in range(header["file_count"]):
        path_len = struct.unpack("<I", f.read(4))[0]
        path_raw = f.read(path_len)
        path = path_raw.rstrip(b"\x00").decode("utf-8", errors="replace")
        offset = struct.unpack("<Q", f.read(8))[0]
        size = struct.unpack("<Q", f.read(8))[0]
        md5 = f.read(16)
        if has_per_file_flags:
            file_flags = struct.unpack("<I", f.read(4))[0]
        else:
            file_flags = 0
        yield {
            "path": path,
            "offset": offset + header["files_base"],
            "size": size,
            "flags": file_flags,
            "md5": md5.hex(),
        }


def matches(path: str, grep: str | None, glob: str | None) -> bool:
    if grep and grep.lower() not in path.lower():
        return False
    if glob and not fnmatch.fnmatchcase(path, glob):
        return False
    return True


def cmd_list(args):
    with open(args.pck, "rb") as f:
        header = read_header(f)
        print(f"Godot {header['godot_version']} PCK, format {header['pack_format']}, "
              f"{header['file_count']:,} files", file=sys.stderr)
        total = matched = 0
        sizes = 0
        type_counts: dict[str, int] = {}
        for entry in walk_directory(f, header):
            total += 1
            if matches(entry["path"], args.grep, args.glob):
                matched += 1
                sizes += entry["size"]
                ext = entry["path"].rsplit(".", 1)[-1].lower() if "." in entry["path"] else "noext"
                type_counts[ext] = type_counts.get(ext, 0) + 1
                if not args.summary:
                    print(f"  {entry['size']:>10d}  {entry['path']}")
        print(f"\nMatched {matched:,}/{total:,} files, "
              f"total size {sizes/1024/1024:.1f} MB", file=sys.stderr)
        if args.summary or matched > 200:
            print("Extension counts:", file=sys.stderr)
            for ext, n in sorted(type_counts.items(), key=lambda x: -x[1])[:15]:
                print(f"  {n:>5d}  .{ext}", file=sys.stderr)


def cmd_extract(args):
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    with open(args.pck, "rb") as f:
        header = read_header(f)
        # Collect entries first so we can re-seek
        entries = [
            e for e in walk_directory(f, header)
            if matches(e["path"], args.grep, args.glob)
        ]
        if not entries:
            print("No matching files.", file=sys.stderr)
            return
        print(f"Extracting {len(entries):,} files to {out_root}/ …",
              file=sys.stderr)
        n_written = 0
        for entry in entries:
            # Strip the leading "res://" if present (Godot resource scheme).
            rel = entry["path"]
            if rel.startswith("res://"):
                rel = rel[6:]
            out_path = out_root / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            f.seek(entry["offset"])
            data = f.read(entry["size"])
            with open(out_path, "wb") as g:
                g.write(data)
            n_written += 1
            if n_written % 100 == 0:
                print(f"  {n_written:,}/{len(entries):,}", file=sys.stderr)
        print(f"Done. Wrote {n_written:,} files.", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["list", "LIST", "extract", "EXTRACT"],
                   help="list paths or extract matching files")
    p.add_argument("--pck", default=DEFAULT_PCK,
                   help="path to .pck (default: STS2 install)")
    p.add_argument("--grep", default=None, help="substring filter (case-insens)")
    p.add_argument("--glob", default=None,
                   help='shell-style glob filter, e.g. "*.png"')
    p.add_argument("--out", default="data/sts2_assets",
                   help="extract destination (extract mode only)")
    p.add_argument("--summary", action="store_true",
                   help="only show extension counts, not full path list")
    args = p.parse_args()
    if args.mode.lower() == "list":
        cmd_list(args)
    else:
        cmd_extract(args)


if __name__ == "__main__":
    main()
