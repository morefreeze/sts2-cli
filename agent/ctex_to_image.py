#!/usr/bin/env python3
"""ctex_to_image.py — Convert Godot 4 .ctex files to .webp / .png.

Godot wraps imported images in a "GST2" container with a small binary
header followed by a raw payload (WebP, PNG, S3TC/BC1-BC7 DDS data, etc).
For the common case where the payload is WebP we just strip the header
and write the .webp; the file then opens in any modern viewer / Pillow /
browser.

Scans the input dir for `*.ctex`, decodes each, and writes the payload
beside it (or to --out). Files whose payload is compressed (S3TC/BPTC)
need a separate decoder — flagged in the report rather than written.

Usage:
    .venv/bin/python agent/ctex_to_image.py <dir_or_file> [--out OUT] [--convert-png]

With --convert-png, WebP payloads are loaded via Pillow and re-saved as
PNG for tools that don't speak WebP. Otherwise raw .webp is written.
"""
import argparse
import os
import struct
import sys
from pathlib import Path


def _find_payload_offset(data: bytes) -> tuple[int, str] | tuple[None, str]:
    """Locate the raw image payload inside a .ctex blob.
    Returns (offset, kind) where kind is 'webp', 'png', or 'unknown'.
    """
    if not data.startswith(b"GST2"):
        return None, "not_ctex"
    # Search the first 256 header bytes for a known image magic.
    head = data[:512]
    webp = head.find(b"RIFF")
    # Confirm it's a WebP (RIFF + size + WEBP)
    if webp >= 0 and webp + 12 <= len(head) and head[webp + 8 : webp + 12] == b"WEBP":
        return webp, "webp"
    png = head.find(b"\x89PNG\r\n\x1a\n")
    if png >= 0:
        return png, "png"
    return None, "unknown"


def convert(path: Path, out_dir: Path, convert_png: bool) -> dict:
    data = path.read_bytes()
    offset, kind = _find_payload_offset(data)
    if offset is None:
        return {"path": str(path), "status": "skip", "reason": kind}
    payload = data[offset:]
    # Output name: strip the cache-hash + .ctex (or .s3tc.ctex) from the name.
    name = path.name
    # Cache filenames look like "foo.png-{hash}.ctex" or "...s3tc.ctex"
    # Reduce to "foo.<webp|png>"
    base = name.split("-")[0] if "-" in name else name
    if base.endswith(".png"):
        base = base[:-4]  # drop trailing .png (original asset name)
    ext = "webp" if kind == "webp" else "png"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{base}.{ext}"
    out_path.write_bytes(payload)
    result = {"path": str(path), "out": str(out_path),
              "kind": kind, "bytes": len(payload), "status": "ok"}
    if convert_png and kind == "webp":
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(payload))
            png_path = out_path.with_suffix(".png")
            img.save(png_path, "PNG")
            result["png_out"] = str(png_path)
        except Exception as e:
            result["png_err"] = str(e)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("input", help="directory or single .ctex file")
    p.add_argument("--out", default="data/sts2_images",
                   help="output directory")
    p.add_argument("--convert-png", action="store_true",
                   help="also save WebP payloads as PNG (requires Pillow)")
    args = p.parse_args()

    inp = Path(args.input)
    out = Path(args.out)
    files: list[Path] = []
    if inp.is_dir():
        files = sorted(inp.rglob("*.ctex"))
    elif inp.is_file():
        files = [inp]
    else:
        print(f"Not found: {inp}", file=sys.stderr)
        sys.exit(1)

    n_ok = n_skip = 0
    skip_kinds: dict[str, int] = {}
    for f in files:
        r = convert(f, out, args.convert_png)
        if r["status"] == "ok":
            n_ok += 1
            if n_ok <= 5 or n_ok % 50 == 0:
                print(f"  {r['kind']:<5s}  {r['bytes']:>9d}B  {r['out']}")
        else:
            n_skip += 1
            k = r.get("reason", "?")
            skip_kinds[k] = skip_kinds.get(k, 0) + 1
    print(f"\nDone. {n_ok} converted, {n_skip} skipped.")
    if skip_kinds:
        for k, n in skip_kinds.items():
            print(f"  skipped {n}: {k}")


if __name__ == "__main__":
    main()
