import os
import socket
from datetime import datetime, timezone
import email.utils
import threading


HOST = '127.0.0.1'
PORT = 8080

def make_response(status_code, status_text, body_content="", content_type="text/html; charset=utf-8", extra_headers=""):
    body_bytes = body_content.encode('utf-8')
    header = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n"
        f"{extra_headers}"
        "\r\n"
    )
    return header.encode('utf-8') + body_bytes


def handle_client(client_socket, client_address):
    try:
        request_data = client_socket.recv(8192).decode('utf-8', errors='ignore')
        
        if not request_data.strip():
            client_socket.close()
            return

        lines = request_data.split('\r\n')
        first_line = lines[0]
        
        parts = first_line.split(' ')
        if len(parts) < 3:
            client_socket.close()
            return

        method = parts[0]
        path = parts[1]
        version = parts[2]


        print(f"[{threading.current_thread().name}] Method: {method}, Path: {path}, Version: {version}", flush=True)

        # 1. 505 HTTP Version Not Supported
        if version != "HTTP/1.1":
            response = make_response(505, "HTTP Version Not Supported", "<h1>505 HTTP Version Not Supported</h1>")
            client_socket.sendall(response)
            client_socket.close()
            return

        # path 
        if path == '/':
            path = '/test.html'

        file_path = os.path.join('www', path.lstrip('/'))

        # 2. 403 Forbidden
        if "secret" in file_path:
            response = make_response(403, "Forbidden", "<h1>403 Forbidden</h1>")
            client_socket.sendall(response)
            client_socket.close()
            return

        # 3. 404 Not Found
        if not os.path.exists(file_path):
            response = make_response(404, "Not Found", "<h1>404 Not Found</h1>")
            client_socket.sendall(response)
            client_socket.close()
            return

        # 4. 304 Not Modified
        if_modified_since_header = None
        for line in lines:
            if line.lower().startswith("if-modified-since:"):
                if_modified_since_header = line.split(":", 1)[1].strip()
                break

        if if_modified_since_header:
            try:
                header_time = email.utils.parsedate_to_datetime(if_modified_since_header)
                file_mtime_epoch = os.path.getmtime(file_path)

                if int(file_mtime_epoch) <= int(header_time.timestamp()):
                    response = make_response(304, "Not Modified", "")
                    client_socket.sendall(response)
                    client_socket.close()
                    return
            except Exception as e:
                print(f"[304 Check Error]: {e}", flush=True)

        # 5. 200 OK
        with open(file_path, 'r', encoding='utf-8') as f:
            body = f.read()

        response = make_response(200, "OK", body)
        client_socket.sendall(response)
        client_socket.close()

    except Exception as e:
        print(f"[Error handling client]: {e}", flush=True)
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
    #make new thread for every new client
    client_thread = threading.Thread(target=handle_client, args=(client_socket, client_address))
    client_thread.start()