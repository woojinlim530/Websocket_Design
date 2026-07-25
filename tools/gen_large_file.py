import os
import sys

def gen_large_file(path, size_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    chunk = b"A" * 1024 * 1024  # 1 MiB chunk
    written = 0
    with open(path, "wb") as f:
        while written < size_bytes:
            to_write = min(len(chunk), size_bytes - written)
            f.write(chunk[:to_write])
            written += to_write

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python3 tools/gen_large_file.py <output-path> <size-bytes>")
        sys.exit(1)
    out = sys.argv[1]
    size = int(sys.argv[2])
    gen_large_file(out, size)
    print(f"Wrote {size} bytes to {out}")
