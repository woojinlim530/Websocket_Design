import socket
import struct
import threading

HOST = "127.0.0.1"
PORT = 8080
HEADER_FORMAT = ">III"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

FRAME_REQUEST = 1
FRAME_RESPONSE = 2
FRAME_RESPONSE_END = 3
FRAME_ERROR = 4


def read_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Connection closed while reading")
        data += chunk
    return data


class FramedConnection:
    def __init__(self, sock):
        self.sock = sock
        self.send_lock = threading.Lock()

    def send_frame(self, frame_type, stream_id, payload=b""):
        header = struct.pack(HEADER_FORMAT, len(payload), frame_type, stream_id)
        with self.send_lock:
            self.sock.sendall(header + payload)

    def recv_frame(self):
        header = read_exact(self.sock, HEADER_SIZE)
        length, frame_type, stream_id = struct.unpack(HEADER_FORMAT, header)
        payload = read_exact(self.sock, length) if length else b""
        return frame_type, stream_id, payload


def make_http_request(host, path="/", method="GET"):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: keep-alive", "", ""]
    return "\r\n".join(lines).encode("iso-8859-1")


def read_responses(framed, results):
    while True:
        try:
            frame_type, stream_id, payload = framed.recv_frame()
        except ConnectionError:
            break

        if frame_type == FRAME_RESPONSE:
            results.setdefault(stream_id, 0)
            results[stream_id] += len(payload)
        elif frame_type == FRAME_RESPONSE_END:
            print(f"Stream {stream_id} complete, {results.get(stream_id, 0)} bytes")
        elif frame_type == FRAME_ERROR:
            print(f"Stream {stream_id} error: {payload.decode('utf-8', errors='replace')}")
        else:
            print(f"Unknown frame {frame_type} on stream {stream_id}")

    return results


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        framed = FramedConnection(sock)

        urls = [
            (1, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nConnection: keep-alive\r\n\r\n"),
            (2, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nConnection: keep-alive\r\n\r\n"),
        ]

        for stream_id, request in urls:
            framed.send_frame(FRAME_REQUEST, stream_id, request)

        results = {}
        read_thread = threading.Thread(target=read_responses, args=(framed, results), daemon=True)
        read_thread.start()
        read_thread.join(timeout=30)

        # Clear summary output
        print("--- Summary ---")
        for sid in sorted(results):
            print(f"Stream {sid}: {results[sid]} bytes")


if __name__ == "__main__":
    main()
