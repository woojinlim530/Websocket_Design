import socket
import struct
import threading
import time

HOST = "127.0.0.1"
PROXY_PORT = 8080
ORIGIN_HOST = "127.0.0.1"
ORIGIN_PORT = 8000

HEADER_FMT = ">III"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
FRAME_REQUEST = 1
FRAME_RESPONSE = 2
FRAME_RESPONSE_END = 3
FRAME_ERROR = 4


def read_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def send_frame(sock, frame_type, stream_id, payload=b""):
    header = struct.pack(HEADER_FMT, len(payload), frame_type, stream_id)
    sock.sendall(header + payload)


def recv_frame(sock):
    hdr = read_exact(sock, HEADER_SIZE)
    length, frame_type, stream_id = struct.unpack(HEADER_FMT, hdr)
    payload = read_exact(sock, length) if length else b""
    return frame_type, stream_id, payload


def make_req(path):
    req = (
        f"GET http://{ORIGIN_HOST}:{ORIGIN_PORT}{path} HTTP/1.1\r\n"
        f"Host: {ORIGIN_HOST}:{ORIGIN_PORT}\r\n"
        "Connection: keep-alive\r\n\r\n"
    )
    return req.encode("iso-8859-1")


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PROXY_PORT))

    results = {}
    first_byte_ts = {}
    end_ts = {}
    events = {1: threading.Event(), 2: threading.Event()}

    def reader():
        try:
            while True:
                frame_type, stream_id, payload = recv_frame(s)
                now = time.time()
                if frame_type == FRAME_RESPONSE:
                    results.setdefault(stream_id, 0)
                    results[stream_id] += len(payload)
                    first_byte_ts.setdefault(stream_id, now)
                elif frame_type == FRAME_RESPONSE_END:
                    end_ts[stream_id] = now
                    events[stream_id].set()
                elif frame_type == FRAME_ERROR:
                    print(f"Stream {stream_id} error: {payload.decode(errors='replace')}")
                    events[stream_id].set()
        except ConnectionError:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Send large request first
    req1 = make_req('/large.bin')
    send_frame(s, FRAME_REQUEST, 1, req1)
    # small delay, then send small request
    time.sleep(0.1)
    send_time = time.time()
    req2 = make_req('/small.txt')
    send_frame(s, FRAME_REQUEST, 2, req2)

    # wait for small to complete
    events[2].wait(timeout=30)

    if 2 in first_byte_ts:
        ttfb = first_byte_ts[2] - send_time
        total = end_ts.get(2, time.time()) - send_time
        print(f"Small stream: bytes={results.get(2,0)}, ttfb={ttfb:.3f}s total={total:.3f}s")
    else:
        print("No response for small stream")

    # wait for large to finish (optional)
    events[1].wait(timeout=30)
    print(f"Large stream: bytes={results.get(1,0)}")

    s.close()


if __name__ == '__main__':
    print("Test will send a large request then a small request over one framed connection.")
    print("Start the origin server: python3 -m http.server 8000 --directory www")
    print("Start the proxy: python3 web_server.py")
    input("Press Enter to run test...")
    main()
