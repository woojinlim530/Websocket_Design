import mimetypes
import os
import socket
import threading
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

HOST = "0.0.0.0"
PORT = 8080
BUFFER_SIZE = 8192
DOCUMENT_ROOT = Path(__file__).parent / "www"

STATUS_MESSAGES = {
    200: "OK",
    304: "Not Modified",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    505: "HTTP Version Not Supported",
}


def receive_request(client_socket):
    """Receive one complete HTTP request header."""
    request = b""

    while b"\r\n\r\n" not in request:
        data = client_socket.recv(BUFFER_SIZE)
        if not data:
            break
        request += data

    return request


def parse_request(request):
    """Extract method, target, version, and headers from the request."""
    text = request.decode("iso-8859-1")
    lines = text.split("\r\n")

    request_line = lines[0].split()
    if len(request_line) != 3:
        raise ValueError("Invalid request line")

    method, target, version = request_line
    headers = {}

    for line in lines[1:]:
        if not line:
            break
        if ":" not in line:
            raise ValueError("Invalid header")

        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    return method.upper(), target, version, headers


def make_error_page(code, message):
    """Create a small HTML page for an error response."""
    return f"""<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <title>{code} {message}</title>
</head>
<body>
    <h1>{code} {message}</h1>
    <p>The server could not complete the request.</p>
</body>
</html>
""".encode("utf-8")


def send_response(
    client_socket,
    status_code,
    body=b"",
    content_type="text/html; charset=utf-8",
    extra_headers=None,
    send_body=True,
):
    """Build and send an HTTP/1.1 response."""
    message = STATUS_MESSAGES[status_code]

    headers = {
        "Date": formatdate(usegmt=True),
        "Server": "CMPT371-WebServer/1.0",
        "Connection": "close",
        "Content-Length": str(len(body)),
    }

    if status_code != 304:
        headers["Content-Type"] = content_type

    if extra_headers:
        headers.update(extra_headers)

    response_lines = [f"HTTP/1.1 {status_code} {message}"]
    response_lines.extend(f"{name}: {value}" for name, value in headers.items())

    response_header = ("\r\n".join(response_lines) + "\r\n\r\n").encode(
        "iso-8859-1"
    )

    client_socket.sendall(response_header)

    if send_body and body and status_code != 304:
        client_socket.sendall(body)


def get_requested_file(target):
    """Convert the requested URL into a safe file path inside www."""
    parsed_target = urlsplit(target)
    request_path = unquote(parsed_target.path)

    if "\x00" in request_path:
        return None, True

    path_parts = Path(request_path).parts
    if ".." in path_parts:
        return None, True

    if request_path == "/":
        request_path = "/index.html"

    relative_path = request_path.lstrip("/")
    requested_file = (DOCUMENT_ROOT / relative_path).resolve()
    document_root = DOCUMENT_ROOT.resolve()

    try:
        requested_file.relative_to(document_root)
    except ValueError:
        return None, True

    if requested_file.exists() and requested_file.is_dir():
        return None, True

    return requested_file, False


def file_not_modified(headers, modified_time):
    """Check whether the browser already has the newest file version."""
    value = headers.get("if-modified-since")

    if not value:
        return False

    try:
        browser_time = parsedate_to_datetime(value).timestamp()
        return int(modified_time) <= int(browser_time)
    except (TypeError, ValueError, OverflowError):
        return False


def handle_client(client_socket, client_address):
    """Handle one browser connection."""
    try:
        request = receive_request(client_socket)

        if not request:
            return

        try:
            method, target, version, headers = parse_request(request)
        except ValueError:
            body = make_error_page(400, STATUS_MESSAGES[400])
            send_response(client_socket, 400, body)
            return

        print(f"{client_address} -> {method} {target} {version}")

        if version != "HTTP/1.1":
            body = make_error_page(505, STATUS_MESSAGES[505])
            send_response(client_socket, 505, body)
            return

        if method not in {"GET", "HEAD"}:
            body = make_error_page(405, STATUS_MESSAGES[405])
            send_response(
                client_socket,
                405,
                body,
                extra_headers={"Allow": "GET, HEAD"},
                send_body=(method != "HEAD"),
            )
            return

        requested_file, forbidden = get_requested_file(target)

        if forbidden:
            body = make_error_page(403, STATUS_MESSAGES[403])
            send_response(client_socket, 403, body, send_body=(method != "HEAD"))
            return

        if requested_file is None or not requested_file.exists():
            body = make_error_page(404, STATUS_MESSAGES[404])
            send_response(client_socket, 404, body, send_body=(method != "HEAD"))
            return

        if not requested_file.is_file():
            body = make_error_page(403, STATUS_MESSAGES[403])
            send_response(client_socket, 403, body, send_body=(method != "HEAD"))
            return

        modified_time = requested_file.stat().st_mtime
        last_modified = formatdate(modified_time, usegmt=True)

        if file_not_modified(headers, modified_time):
            send_response(
                client_socket,
                304,
                extra_headers={"Last-Modified": last_modified},
                send_body=False,
            )
            return

        try:
            body = requested_file.read_bytes()
        except PermissionError:
            body = make_error_page(403, STATUS_MESSAGES[403])
            send_response(client_socket, 403, body, send_body=(method != "HEAD"))
            return

        content_type, _ = mimetypes.guess_type(str(requested_file))
        if content_type is None:
            content_type = "application/octet-stream"
        elif content_type.startswith("text/"):
            content_type += "; charset=utf-8"

        send_response(
            client_socket,
            200,
            body,
            content_type=content_type,
            extra_headers={"Last-Modified": last_modified},
            send_body=(method != "HEAD"),
        )

    except (ConnectionError, OSError) as error:
        print(f"Connection error from {client_address}: {error}")
    finally:
        client_socket.close()


def start_server():
    """Start the multithreaded web server."""
    DOCUMENT_ROOT.mkdir(exist_ok=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(50)

        print(f"Web server is running on http://127.0.0.1:{PORT}")
        print(f"Serving files from: {DOCUMENT_ROOT}")
        print("Press Control+C to stop it.")

        try:
            while True:
                client_socket, client_address = server_socket.accept()

                client_thread = threading.Thread(
                    target=handle_client,
                    args=(client_socket, client_address),
                    daemon=True,
                )
                client_thread.start()
        except KeyboardInterrupt:
            print("\nWeb server stopped.")


if __name__ == "__main__":
    start_server()