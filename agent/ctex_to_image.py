#!/usr/bin/env python3
"""ctex_to_image.py — Convert Godot 4 .ctex files to PNG / WebP.

Godot wraps imported images in a "GST2" container. Three payload paths:

- WebP — strip the 32-byte GST2 wrapper, written as .webp (or re-saved
  as PNG with --convert-png).
- BC3 (DXT5, .s3tc.ctex) or BC7 (.bptc.ctex) — decoded via
  texture2ddecoder into RGBA pixels and saved as PNG.

GST2 layout (format version 1 — Godot 4.5):
    0x00  "GST2"            4
    0x04  version=1         4
    0x08  display_w         4 (display width — may be < padded block width)
    0x0c  display_h         4
    0x10  flags             4
    0x14  mipmap_limit      4
    0x18  reserved          12
    0x24  ??? (zero)        4
    0x28  packed dims       4 (padded w/h as 2x u16, undocumented)
    0x2c  ??? (zero)        4
    0x30  Image::Format     4 (0x11=DXT1, 0x13=DXT5, 0x16=BPTC_RGBA)
    0x34  compressed data   to end

For BC3/BC7 the payload size matches (padded_w/4) * (padded_h/4) * 16,
which confirms the data starts at 0x34 with no extra footer.

Usage:
    .venv/bin/python agent/ctex_to_image.py <dir_or_file> [--out OUT] [--convert-png]
"""
import argparse
import os
import struct
import sys
from pathlib import Path


_GODOT_FMT_DXT1 = 0x11
_GODOT_FMT_DXT5 = 0x13
_GODOT_FMT_BPTC_RGBA = 0x16


def _find_payload_offset(data: bytes) -> tuple[int, str] | tuple[None, str]:
    """Locate the raw image payload inside a .ctex blob.
    Returns (offset, kind) where kind is 'webp', 'png',
    'bc3'/'bc7'/'bc1' for compressed, or 'unknown'.
    """
    if not data.startswith(b"GST2"):
        return None, "not_ctex"
    head = data[:512]
    webp = head.find(b"RIFF")
    if webp >= 0 and webp + 12 <= len(head) and head[webp + 8 : webp + 12] == b"WEBP":
        return webp, "webp"
    png = head.find(b"\x89PNG\r\n\x1a\n")
    if png >= 0:
        return png, "png"
    # Compressed payload: read Image::Format at 0x30.
    if len(data) >= 0x34:
        fmt = struct.unpack("<I", data[0x30:0x34])[0]
        if fmt == _GODOT_FMT_DXT5:
            return 0x34, "bc3"
        if fmt == _GODOT_FMT_BPTC_RGBA:
            return 0x34, "bc7"
        if fmt == _GODOT_FMT_DXT1:
            return 0x34, "bc1"
    return None, "unknown"


def _decode_compressed(data: bytes, payload_offset: int, kind: str) -> "Image.Image":
    """Decode a BC1/BC3/BC7 ctex into a PIL RGBA Image.

    The visible image is `display_w x display_h`; the compressed data is
    laid out for the next-multiple-of-4 padded dims. We decode the full
    padded block, then crop back to display dims.
    """
    import texture2ddecoder  # type: ignore[import-not-found]
    from PIL import Image

    display_w = struct.unpack("<I", data[8:12])[0]
    display_h = struct.unpack("<I", data[12:16])[0]
    pw = (display_w + 3) & ~3
    ph = (display_h + 3) & ~3
    payload = data[payload_offset:]
    if kind == "bc3":
        rgba = texture2ddecoder.decode_bc3(payload, pw, ph)
    elif kind == "bc7":
        rgba = texture2ddecoder.decode_bc7(payload, pw, ph)
    elif kind == "bc1":
        rgba = texture2ddecoder.decode_bc1(payload, pw, ph)
    else:
        raise ValueError(f"Unsupported compressed kind: {kind}")
    # texture2ddecoder returns BGRA byte order — swap to RGBA.
    img = Image.frombytes("RGBA", (pw, ph), rgba, "raw", "BGRA")
    if (display_w, display_h) != (pw, ph):
        img = img.crop((0, 0, display_w, display_h))
    return img


def convert(path: Path, out_dir: Path, convert_png: bool) -> dict:
    data = path.read_bytes()
    offset, kind = _find_payload_offset(data)
    if offset is None:
        return {"path": str(path), "status": "skip", "reason": kind}
    # Output name: strip the cache-hash + .ctex (or .s3tc.ctex) from the name.
    name = path.name
    base = name.split("-")[0] if "-" in name else name
    if base.endswith(".png"):
        base = base[:-4]
    out_dir.mkdir(parents=True, exist_ok=True)
    if kind in ("bc3", "bc7", "bc1"):
        try:
            img = _decode_compressed(data, offset, kind)
        except Exception as e:
            return {"path": str(path), "status": "skip", "reason": f"{kind}_err:{e}"}
        out_path = out_dir / f"{base}.png"
        img.save(out_path, "PNG")
        return {"path": str(path), "out": str(out_path), "kind": kind,
                "bytes": len(data) - offset, "status": "ok"}
    payload = data[offset:]
    ext = "webp" if kind == "webp" else "png"
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
