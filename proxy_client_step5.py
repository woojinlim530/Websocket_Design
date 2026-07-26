import socket
import struct
import threading
import time

HOST = '127.0.0.1'
PORT = 18080
HEADER_FMT = '>III'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
FRAME_REQUEST = 1
FRAME_RESPONSE = 2
FRAME_RESPONSE_END = 3
FRAME_ERROR = 4
BUFFER_SIZE = 8192


def send_frame(sock, frame_type, stream_id, payload=b''):
    header = struct.pack(HEADER_FMT, len(payload), frame_type, stream_id)
    sock.sendall(header + payload)


def read_exact(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError('closed')
        data += chunk
    return data


def recv_frame(sock):
    hdr = read_exact(sock, HEADER_SIZE)
    length, frame_type, stream_id = struct.unpack(HEADER_FMT, hdr)
    payload = read_exact(sock, length) if length else b''
    return frame_type, stream_id, payload


def make_req(path, host='127.0.0.1', port=8000):
    req = (
        f"GET http://{host}:{port}{path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: keep-alive\r\n\r\n"
    )
    return req.encode('iso-8859-1')


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))

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

    # Send two requests multiplexed
    req1 = make_req('/large.bin')
    send_frame(s, FRAME_REQUEST, 1, req1)
    time.sleep(0.0001)
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
        print('No response for small stream')

    # wait for large to finish (optional)
    events[1].wait(timeout=30)
    print(f"Large stream: bytes={results.get(1,0)}")

    s.close()


if __name__ == '__main__':
    print('Step 5 framed test: start origin server: python -m http.server 8000 --directory www')
    print('Start server: python web_server_step5.py')
    input('Press Enter to run test...')
    main()
