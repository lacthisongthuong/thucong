#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tai.py — Tải HLS segment ngụy trang PNG, cắt header, ghép bằng ffmpeg.

Chạy không tham số -> hỏi tương tác:
    py tai.py

Hoặc truyền tham số:
    py tai.py --m3u8 462.txt --output video.mp4
    py tai.py --check seg0.png
"""

import argparse
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# --------------------------------------------------------------------------
# Cắt header PNG
# --------------------------------------------------------------------------
def strip_png_header(data: bytes, verbose: bool = False) -> bytes:
    iend = data.find(b"\x00\x00\x00\x00IEND")
    if iend != -1:
        start = iend + 12
        if verbose:
            print(f"  [check] IEND length-field @ 0x{iend:04X}, "
                  f"'IEND' @ 0x{iend+4:04X}, CRC @ 0x{iend+8:04X}, "
                  f"TS start @ 0x{start:04X} (={start})")
        if start < len(data) and data[start] == 0x47:
            return data[start:]
        for i in range(len(data) - 376):
            if data[i] == 0x47 and data[i+188] == 0x47 and data[i+376] == 0x47:
                if verbose:
                    print(f"  [check] fallback: TS @ 0x{i:04X} (={i})")
                return data[i:]
        if verbose:
            print("  [check] IEND found nhưng không thấy 0x47 — trả nguyên")
        return data
    if verbose:
        print("  [check] Không có IEND — coi như đã là TS thuần")
    return data


def check_one(path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    print(f"File: {path}  size={len(data)} bytes")
    print(f"  16 byte đầu: {data[:16].hex(' ').upper()}")
    ts = strip_png_header(data, verbose=True)
    print(f"  Sau khi cắt: size={len(ts)} bytes, "
          f"mod188={len(ts) % 188} (0 là chuẩn)")
    print(f"  16 byte đầu TS: {ts[:16].hex(' ').upper()}")
    if len(ts) >= 4 and ts[0] == 0x47:
        pid = ((ts[1] & 0x1F) << 8) | ts[2]
        print(f"  Gói TS đầu: sync=0x47, PID=0x{pid:04X}")
    else:
        print("  ⚠️  Byte đầu không phải 0x47 — cắt có thể sai!")


# --------------------------------------------------------------------------
# Tải segment
# --------------------------------------------------------------------------
def download_one(args):
    idx, url, out_dir, do_strip = args
    base = f"{idx:05d}"
    raw_path = os.path.join(out_dir, base + ".png")
    ts_path = os.path.join(out_dir, base + ".ts")

    if not (os.path.exists(ts_path) and os.path.getsize(ts_path) > 0):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        with open(raw_path, "wb") as f:
            f.write(raw)
        if do_strip:
            ts = strip_png_header(raw)
            with open(ts_path, "wb") as f:
                f.write(ts)
        else:
            ts_path = raw_path
    return idx, ts_path


def parse_m3u8(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]
    segments = [l.strip() for l in lines
                if l.strip() and not l.strip().startswith("#")]
    return lines, segments


def make_local_m3u8(orig_lines, out_dir, local_path, do_strip):
    out = []
    seg_i = 0
    for l in orig_lines:
        s = l.strip()
        if s.startswith("#EXTINF"):
            out.append(l)
        elif s.startswith("#EXTM3U") or s.startswith("#EXT-X-"):
            out.append(l)
        elif s == "":
            out.append(l)
        elif s.startswith("#"):
            if s == "#EXT-X-ENDLIST":
                out.append(l)
        else:
            ext = ".ts" if do_strip else ".png"
            fpath = os.path.abspath(os.path.join(out_dir, f"{seg_i:05d}{ext}"))
            out.append(fpath)
            seg_i += 1
    with open(local_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return seg_i


# --------------------------------------------------------------------------
# Hỏi tương tác
# --------------------------------------------------------------------------
def ask_inputs():
    print("=== HLS PNG Fix ===")
    while True:
        m3u8 = input("Đường dẫn file m3u8: ").strip().strip('"').strip("'")
        if not m3u8:
            print("  ⚠️  Không được để trống.")
            continue
        if not os.path.isfile(m3u8):
            print(f"  ⚠️  Không thấy file: {m3u8}")
            continue
        break

    default_out = os.path.splitext(os.path.basename(m3u8))[0] + ".mp4"
    while True:
        out = input(f"Tên file output (mặc định {default_out}): ").strip()
        out = out.strip('"').strip("'")
        if not out:
            out = default_out
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
        # nếu chỉ nhập tên, đặt cạnh m3u8
        if not os.path.isabs(out) and os.path.dirname(out) == "":
            out = os.path.join(os.path.dirname(os.path.abspath(m3u8)), out)
        break

    return m3u8, out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", help="Kiểm tra 1 file segment đã tải")
    ap.add_argument("--m3u8", help="File m3u8 gốc (bỏ trống để hỏi)")
    ap.add_argument("--output", help="File mp4 đầu ra (bỏ trống để hỏi)")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-strip", action="store_true")
    ap.add_argument("--no-ffmpeg", action="store_true")
    ap.add_argument("--allowed-ext", action="store_true")
    args = ap.parse_args()

    # Chế độ check
    if args.check:
        check_one(args.check)
        return

    # Nếu thiếu m3u8 hoặc output -> hỏi tương tác
    if not args.m3u8 or not args.output:
        m3u8, out = ask_inputs()
        args.m3u8 = args.m3u8 or m3u8
        args.output = args.output or out

    # Chuẩn hóa output
    if not args.output.lower().endswith(".mp4"):
        args.output += ".mp4"
    if not os.path.isabs(args.output) and os.path.dirname(args.output) == "":
        args.output = os.path.join(
            os.path.dirname(os.path.abspath(args.m3u8)), args.output)

    do_strip = not args.no_strip
    out_dir = args.outdir or (os.path.splitext(args.m3u8)[0] + "_segments")
    os.makedirs(out_dir, exist_ok=True)

    orig_lines, segments = parse_m3u8(args.m3u8)
    print(f"\nĐọc {args.m3u8}: {len(segments)} segment")
    print(f"Thư mục segment : {out_dir}")
    print(f"Output          : {args.output}\n")

    jobs = [(i, u, out_dir, do_strip) for i, u in enumerate(segments)]
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(download_one, j) for j in jobs]
        for fut in as_completed(futures):
            idx, path = fut.result()
            done += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"  tải {done}/{len(jobs)} ... (mới nhất idx={idx})")

    local_m3u8 = os.path.splitext(args.m3u8)[0] + "_local.m3u8"
    n = make_local_m3u8(orig_lines, out_dir, local_m3u8, do_strip)
    print(f"Đã tạo {local_m3u8} với {n} segment "
          f"({'ts' if do_strip else 'png'})")

    if args.no_ffmpeg:
        print("Bỏ qua ffmpeg. Tự chạy:")
        print(f"  ffmpeg -i {local_m3u8} -c copy {args.output}")
        return

    cmd = ["ffmpeg", "-y"]
    if args.allowed_ext or not do_strip:
        cmd += ["-allowed_extensions", "ALL"]
    cmd += ["-i", local_m3u8, "-c", "copy", args.output]
    print("Chạy:", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("❌ ffmpeg lỗi. Thử --allowed-ext hoặc --check 1 file.")
        sys.exit(r.returncode)
    print(f"✅ Xong: {args.output}")


if __name__ == "__main__":
    main()
