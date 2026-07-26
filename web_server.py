#deprecated version

import mimetypes
import os
import socket
import concurrent.futures
from datetime import datetime, timezone
import email.utils
import threading
from urllib.parse import urlsplit


HOST = '127.0.0.1'
PORT = 8080
BUFFER_SIZE = 8192
ROOT_DIR = os.path.abspath('.')

SOCKET_TIMEOUT = 10  # seconds for socket operations


def make_response(status_code, status_text, body_content=b"", content_type="text/html; charset=utf-8", extra_headers="", connection_header="close"):
    if isinstance(body_content, str):
        body_bytes = body_content.encode('utf-8')
    else:
        body_bytes = body_content
    header = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: {connection_header}\r\n"
        f"{extra_headers}"
        "\r\n"
    )
    return header.encode('utf-8') + body_bytes


# Removed custom framing protocol and FramedSocket class per assignment requirements.


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
    # Ask upstream to close to simplify proxying behavior
    lines.append('Connection: close')
    lines.append('')
    request_headers = '\r\n'.join(lines).encode('iso-8859-1') + b'\r\n'
    return request_headers + body


def relay_origin_plain(client_sock, host, port, request_bytes):
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
                client_sock.sendall(chunk)
    except Exception as exc:
        try:
            client_sock.sendall(make_response(502, 'Bad Gateway', f'<h1>502 Bad Gateway</h1>\n<p>{exc}</p>\n'))
        except Exception:
            pass


def handle_single_request(client_sock, client_addr, method, target, version, headers, body):
    # Validate HTTP version
    if version not in ('HTTP/1.0', 'HTTP/1.1'):
        client_sock.sendall(make_response(505, 'HTTP Version Not Supported', '<h1>505 HTTP Version Not Supported</h1>\n'))
        return

    # Determine keep-alive semantics
    conn_hdr = headers.get('connection', '')
    keep_alive = False
    if version == 'HTTP/1.1':
        keep_alive = conn_hdr.lower() != 'close'
    else:
        keep_alive = conn_hdr.lower() == 'keep-alive'

    if target.startswith('http://') or target.startswith('https://'):
        try:
            host, port, path = resolve_origin(target, headers)
        except ValueError as exc:
            client_sock.sendall(make_response(400, 'Bad Request', f'<h1>400 Bad Request</h1>\n<p>{exc}</p>\n', connection_header='close'))
            return

        request_bytes = build_origin_request(method, path, version, headers, body, host)
        relay_origin_plain(client_sock, host, port, request_bytes)
        return

    # Build local response once
    response = build_local_response(method, target, version, headers)
    # Ensure connection header matches keep_alive
    conn_value = 'keep-alive' if keep_alive else 'close'
    # Insert connection header by replacing the default Connection: close in response
    if b'Connection:' in response:
        response = response.replace(b'Connection: close\r\n', f'Connection: {conn_value}\r\n'.encode('utf-8'), 1)
    else:
        # prepend connection header
        header_end = response.find(b'\r\n\r\n')
        if header_end != -1:
            response = response[:header_end] + f'\r\nConnection: {conn_value}\r\n'.encode('utf-8') + response[header_end:]

    client_sock.sendall(response)


def recv_http_request(client_sock):
    # Read headers until CRLF CRLF
    data = b''
    while True:
        try:
            chunk = client_sock.recv(BUFFER_SIZE)
        except socket.timeout:
            raise ConnectionError('Socket read timed out while reading headers')
        if not chunk:
            raise ConnectionError('Connection closed by peer')
        data += chunk
        if b'\r\n\r\n' in data:
            break
        if len(data) > 10 * 1024 * 1024:
            raise ValueError('Headers too large')

    header_end = data.find(b'\r\n\r\n')
    header_bytes = data[:header_end + 4]
    remaining = data[header_end + 4:]

    # Parse headers to check for Content-Length
    method, target, version, headers, body = None, None, None, None, b''
    try:
        method, target, version, headers, body = parse_http_request(header_bytes + remaining)
        return method, target, version, headers, body
    except ValueError:
        # Need to read body based on Content-Length
        text = header_bytes.decode('iso-8859-1')
        lines = text.split('\r\n')
        cl = 0
        for line in lines:
            if line.lower().startswith('content-length:'):
                try:
                    cl = int(line.split(':', 1)[1].strip())
                except Exception:
                    cl = 0
                break

        need = cl - len(remaining)
        while need > 0:
            try:
                chunk = client_sock.recv(min(BUFFER_SIZE, need))
            except socket.timeout:
                raise ConnectionError('Socket read timed out while reading body')
            if not chunk:
                raise ConnectionError('Connection closed while reading body')
            remaining += chunk
            need -= len(chunk)

        return parse_http_request(header_bytes + remaining)


def handle_connection(client_socket, client_address):
    print(f"[{threading.current_thread().name}] Connection from {client_address}", flush=True)
    client_socket.settimeout(SOCKET_TIMEOUT)
    try:
        while True:
            try:
                method, target, version, headers, body = recv_http_request(client_socket)
            except ConnectionError:
                break
            except Exception as exc:
                try:
                    client_socket.sendall(make_response(400, 'Bad Request', f'<h1>400 Bad Request</h1>\n<p>{exc}</p>\n', connection_header='close'))
                except Exception:
                    pass
                break

            # Handle the single request synchronously
            try:
                handle_single_request(client_socket, client_address, method, target, version, headers, body)
            except Exception as exc:
                print(f"[Error handling request]: {exc}", flush=True)
                break

            # Determine whether to keep connection alive
            conn_hdr = headers.get('connection', '')
            if version == 'HTTP/1.1':
                if conn_hdr.lower() == 'close':
                    break
                else:
                    # keep-alive by default
                    continue
            else:
                # HTTP/1.0
                if conn_hdr.lower() == 'keep-alive':
                    continue
                else:
                    break
    finally:
        try:
            client_socket.close()
        except Exception:
            pass

# TCP socket gen
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# Socket reuse setting
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

server_socket.bind((HOST, PORT)) # binding IP and Port num
server_socket.listen(5) # backlog = 5

print("listening on:", HOST, ":", PORT)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)
try:
    while True:
        client_socket, client_address = server_socket.accept()
        # Submit the connection handler to the thread pool
        executor.submit(handle_connection, client_socket, client_address)
except KeyboardInterrupt:
    print("Shutting down server", flush=True)
finally:
    executor.shutdown(wait=True)
    try:
        server_socket.close()
    except Exception:
        pass