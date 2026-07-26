import mimetypes
import os
import socket
import struct
from datetime import datetime, timezone
import email.utils
import threading
from urllib.parse import urlsplit


HOST = '127.0.0.1'
PORT = 8080
BUFFER_SIZE = 8192
ROOT_DIR = os.path.abspath('.')

HEADER_FORMAT = '>III'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
FRAME_REQUEST = 1
FRAME_RESPONSE = 2
FRAME_RESPONSE_END = 3
FRAME_ERROR = 4


def make_response(status_code, status_text, body_content=b"", content_type="text/html; charset=utf-8", extra_headers=""):
    if isinstance(body_content, str):
        body_bytes = body_content.encode('utf-8')
    else:
        body_bytes = body_content
    header = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n"
        f"{extra_headers}"
        "\r\n"
    )
    return header.encode('utf-8') + body_bytes


def read_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('Connection closed while reading')
        data += chunk
    return data


class FramedSocket:
    def __init__(self, sock):
        self.sock = sock
        self.send_lock = threading.Lock()

    def recv_frame(self):
        header = read_exact(self.sock, HEADER_SIZE)
        length, frame_type, stream_id = struct.unpack(HEADER_FORMAT, header)
        payload = read_exact(self.sock, length) if length else b""
        return frame_type, stream_id, payload

    def send_frame(self, frame_type, stream_id, payload=b""):
        header = struct.pack(HEADER_FORMAT, len(payload), frame_type, stream_id)
        with self.send_lock:
            self.sock.sendall(header + payload)


def parse_http_request(payload):
    boundary = payload.find(b"\r\n\r\n")
    if boundary < 0:
        raise ValueError('Incomplete HTTP request')

    header_bytes = payload[:boundary]
    body = payload[boundary + 4 :]
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


def format_http_date(dt):
    return email.utils.format_datetime(dt, usegmt=True)


def parse_http_date(value):
    try:
        return email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def is_http_request_start(data):
    try:
        text = data.decode('ascii', errors='ignore').upper()
    except Exception:
        return False
    return any(text.startswith(method) for method in [
        'GET ', 'HEAD ', 'POST ', 'PUT ', 'DELETE ', 'OPTIONS ', 'TRACE ', 'CONNECT '
    ])


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


def build_local_response(method, target, version, headers):
    if method != 'GET':
        return make_response(403, 'Forbidden', '<h1>403 Forbidden</h1>\n')

    file_path = normalize_local_path(target)
    if file_path is None:
        return make_response(403, 'Forbidden', '<h1>403 Forbidden</h1>\n')

    if os.path.isdir(file_path):
        index_path = os.path.join(file_path, 'index.html')
        if os.path.isfile(index_path):
            file_path = index_path
        else:
            return make_response(404, 'Not Found', '<h1>404 Not Found</h1>\n')

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return make_response(404, 'Not Found', '<h1>404 Not Found</h1>\n')

    if_modified_since = headers.get('if-modified-since')
    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)
    last_modified = format_http_date(file_mtime)
    if if_modified_since:
        ims_dt = parse_http_date(if_modified_since)
        if ims_dt is not None:
            ims_dt = ims_dt.astimezone(timezone.utc).replace(microsecond=0)
            if file_mtime.replace(microsecond=0) <= ims_dt:
                extra_headers = f'Last-Modified: {last_modified}\r\n'
                return make_response(304, 'Not Modified', b'', extra_headers=extra_headers)

    content_type, _ = mimetypes.guess_type(file_path)
    if content_type is None:
        content_type = 'application/octet-stream'

    with open(file_path, 'rb') as f:
        body = f.read()

    extra_headers = f'Last-Modified: {last_modified}\r\n'
    return make_response(200, 'OK', body, content_type=content_type, extra_headers=extra_headers)


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
            upstream.connect((host, port))
            upstream.sendall(request_bytes)

            while True:
                chunk = upstream.recv(BUFFER_SIZE)
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
    framed.send_frame(FRAME_RESPONSE, stream_id, response_bytes)
    framed.send_frame(FRAME_RESPONSE_END, stream_id)


def handle_client(client_socket, client_address):
    framed = FramedSocket(client_socket)
    print(f"[{threading.current_thread().name}] Accepted connection from {client_address}", flush=True)

    try:
        while True:
            frame_type, stream_id, payload = framed.recv_frame()
            if frame_type != FRAME_REQUEST:
                framed.send_frame(FRAME_ERROR, stream_id, b'Expected FRAME_REQUEST frame')
                continue

            thread = threading.Thread(
                target=handle_request_stream,
                args=(framed, stream_id, payload),
                daemon=True,
            )
            thread.start()
    except ConnectionError:
        pass
    except Exception as e:
        print(f"[Error handling client]: {e}", flush=True)
    finally:
        client_socket.close()

# TCP socket gen
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# Socket reuse setting
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

server_socket.bind((HOST, PORT)) # binding IP and Port num
server_socket.listen(5) # backlog = 5

print("listening on:", HOST, ":", PORT)

while True:
    client_socket, client_address = server_socket.accept()
    client_thread = threading.Thread(target=handle_client, args=(client_socket, client_address), daemon=True)
    client_thread.start()