import os
import socket
import struct
import threading
import mimetypes
from datetime import datetime, timezone
import email.utils
from urllib.parse import urlsplit

HOST = '127.0.0.1'
PORT = 18080
BUFFER_SIZE = 8192
ROOT_DIR = os.path.abspath('.')
SOCKET_TIMEOUT = 10

HEADER_FMT = '>III'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
FRAME_REQUEST = 1
FRAME_RESPONSE = 2
FRAME_RESPONSE_END = 3
FRAME_ERROR = 4


def format_http_date(dt):
    return email.utils.format_datetime(dt, usegmt=True)


def parse_http_date(value):
    try:
        return email.utils.parsedate_to_datetime(value)
    except Exception:
        return None


def parse_http_request(payload):
    boundary = payload.find(b"\r\n\r\n")
    if boundary < 0:
        raise ValueError('Incomplete HTTP request')

    header_bytes = payload[:boundary]
    body = payload[boundary + 4:]
    text = header_bytes.decode('iso-8859-1')
    lines = text.split('\r\n')

    request_line = lines[0].split()
    if len(request_line) != 3:
        raise ValueError('Malformed request line')

    method, target, version = request_line
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        if ':' not in line:
            raise ValueError('Malformed header line')
        name, value = line.split(':', 1)
        headers[name.strip().lower()] = value.strip()

    return method.upper(), target, version, headers, body


def normalize_local_path(target):
    parsed = urlsplit(target)
    path = parsed.path if parsed.scheme else target
    if not path.startswith('/'):
        path = '/' + path
    if path.endswith('/'):
        path += 'index.html'

    requested_path = os.path.normpath(os.path.join(ROOT_DIR, path.lstrip('/')))
    if requested_path != ROOT_DIR and not requested_path.startswith(ROOT_DIR + os.sep):
        return None
    return requested_path


class FramedSocket:
    def __init__(self, sock):
        self.sock = sock
        self.send_lock = threading.Lock()

    def recv_frame(self):
        # read header
        header = self._read_exact(HEADER_SIZE)
        length, frame_type, stream_id = struct.unpack(HEADER_FMT, header)
        payload = self._read_exact(length) if length else b''
        return frame_type, stream_id, payload

    def send_frame(self, frame_type, stream_id, payload=b''):
        header = struct.pack(HEADER_FMT, len(payload), frame_type, stream_id)
        with self.send_lock:
            self.sock.sendall(header + payload)

    def _read_exact(self, n):
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError('connection closed')
            data += chunk
        return data


def build_local_response(method, target, version, headers):
    if method != 'GET':
        body = '<h1>403 Forbidden</h1>\n'.encode('utf-8')
        header = f'HTTP/1.1 403 Forbidden\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
        return header.encode('utf-8') + body

    file_path = normalize_local_path(target)
    if file_path is None:
        body = '<h1>403 Forbidden</h1>\n'.encode('utf-8')
        header = f'HTTP/1.1 403 Forbidden\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
        return header.encode('utf-8') + body

    if os.path.isdir(file_path):
        index_path = os.path.join(file_path, 'index.html')
        if os.path.isfile(index_path):
            file_path = index_path
        else:
            body = '<h1>404 Not Found</h1>\n'.encode('utf-8')
            header = f'HTTP/1.1 404 Not Found\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
            return header.encode('utf-8') + body

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        body = '<h1>404 Not Found</h1>\n'.encode('utf-8')
        header = f'HTTP/1.1 404 Not Found\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
        return header.encode('utf-8') + body

    if_modified_since = headers.get('if-modified-since')
    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)
    last_modified = format_http_date(file_mtime)
    if if_modified_since:
        ims_dt = parse_http_date(if_modified_since)
        if ims_dt is not None:
            ims_dt = ims_dt.astimezone(timezone.utc).replace(microsecond=0)
            if file_mtime.replace(microsecond=0) <= ims_dt:
                header = f'HTTP/1.1 304 Not Modified\r\nLast-Modified: {last_modified}\r\nConnection: close\r\n\r\n'
                return header.encode('utf-8')

    content_type, _ = mimetypes.guess_type(file_path)
    if content_type is None:
        content_type = 'application/octet-stream'

    with open(file_path, 'rb') as f:
        body = f.read()

    header = (
        f'HTTP/1.1 200 OK\r\n'
        f'Content-Type: {content_type}\r\n'
        f'Content-Length: {len(body)}\r\n'
        f'Last-Modified: {last_modified}\r\n'
        f'Connection: close\r\n'
        '\r\n'
    )
    return header.encode('utf-8') + body


def resolve_origin(target, headers):
    parsed = urlsplit(target)
    if parsed.scheme and parsed.scheme != 'http':
        raise ValueError('Only http:// targets are supported')

    if parsed.hostname:
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
    else:
        host_header = headers.get('host')
        if not host_header:
            raise ValueError('Missing Host header')
        if ':' in host_header:
            host, port_text = host_header.split(':', 1)
            port = int(port_text)
        else:
            host = host_header
            port = 80
        path = target if target.startswith('/') else '/'

    return host, port, path


def build_origin_request(method, path, version, headers, body, host_override):
    lines = [f"{method} {path} {version}"]
    for name, value in headers.items():
        if name in {'connection', 'proxy-connection', 'host'}:
            continue
        lines.append(f"{name}: {value}")

    lines.append(f"Host: {host_override}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines.append('Connection: close')
    lines.append('')
    request_headers = '\r\n'.join(lines).encode('iso-8859-1') + b'\r\n'
    return request_headers + body


def relay_origin_response(framed, stream_id, host, port, request_bytes):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(SOCKET_TIMEOUT)
            upstream.connect((host, port))
            upstream.sendall(request_bytes)

            while True:
                try:
                    chunk = upstream.recv(BUFFER_SIZE)
                except socket.timeout:
                    break
                if not chunk:
                    break
                framed.send_frame(FRAME_RESPONSE, stream_id, chunk)
    except Exception as exc:
        framed.send_frame(FRAME_ERROR, stream_id, f"Upstream proxy error: {exc}\n".encode('utf-8'))
    finally:
        framed.send_frame(FRAME_RESPONSE_END, stream_id)


def handle_request_stream(framed, stream_id, payload):
    try:
        method, target, version, headers, body = parse_http_request(payload)
    except ValueError as exc:
        framed.send_frame(FRAME_ERROR, stream_id, str(exc).encode('utf-8'))
        framed.send_frame(FRAME_RESPONSE_END, stream_id)
        return

    if version != 'HTTP/1.1':
        response_bytes = make_response(505, 'HTTP Version Not Supported', '<h1>505 HTTP Version Not Supported</h1>\n')
        framed.send_frame(FRAME_RESPONSE, stream_id, response_bytes)
        framed.send_frame(FRAME_RESPONSE_END, stream_id)
        return

    if target.startswith('http://') or target.startswith('https://'):
        try:
            host, port, path = resolve_origin(target, headers)
        except ValueError as exc:
            framed.send_frame(FRAME_ERROR, stream_id, str(exc).encode('utf-8'))
            framed.send_frame(FRAME_RESPONSE_END, stream_id)
            return

        request_bytes = build_origin_request(method, path, version, headers, body, host)
        relay_origin_response(framed, stream_id, host, port, request_bytes)
        return

    response_bytes = build_local_response(method, target, version, headers)
    # send response in chunks
    try:
        for i in range(0, len(response_bytes), BUFFER_SIZE):
            framed.send_frame(FRAME_RESPONSE, stream_id, response_bytes[i:i+BUFFER_SIZE])
    except Exception as exc:
        framed.send_frame(FRAME_ERROR, stream_id, f"Server error: {exc}\n".encode('utf-8'))
    finally:
        framed.send_frame(FRAME_RESPONSE_END, stream_id)


def handle_client(conn, addr):
    framed = FramedSocket(conn)
    conn.settimeout(SOCKET_TIMEOUT)
    print(f"[Framed] Accepted connection from {addr}")
    try:
        while True:
            frame_type, stream_id, payload = framed.recv_frame()
            if frame_type != FRAME_REQUEST:
                framed.send_frame(FRAME_ERROR, stream_id, b'Expected FRAME_REQUEST')
                continue
            # spawn a thread per stream to avoid HOL
            t = threading.Thread(target=handle_request_stream, args=(framed, stream_id, payload), daemon=True)
            t.start()
    except ConnectionError:
        pass
    except Exception as exc:
        print(f"[Framed] connection error: {exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(5)
        print(f'Listening (framed) on {HOST}:{PORT}')
        try:
            while True:
                conn, addr = server.accept()
                th = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                th.start()
        except KeyboardInterrupt:
            print('Shutting down')
