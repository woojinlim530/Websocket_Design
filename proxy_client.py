import socket
import threading

HOST = "127.0.0.1"
PORT = 8080
BUFFER_SIZE = 8192



def make_http_request(host, path="/", method="GET"):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: keep-alive", "", ""]
    return "\r\n".join(lines).encode("iso-8859-1")


def read_responses(sock):
    """Read raw HTTP responses from the socket and print them as text until the connection closes."""
    received = b""
    try:
        while True:
            chunk = sock.recv(BUFFER_SIZE)
            if not chunk:
                break
            received += chunk
            # Print progressively to avoid huge memory use for long responses
            try:
                text = chunk.decode('iso-8859-1')
            except Exception:
                text = chunk.decode('utf-8', errors='replace')
            print(text, end='')
    except ConnectionError:
        pass
    except Exception as exc:
        print(f"Error reading responses: {exc}")
    return received


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        # Prepare two simple HTTP requests; send raw bytes without any framing
        requests = [
            make_http_request('example.com', '/'),
            make_http_request('example.com', '/'),
        ]

        for req in requests:
            sock.sendall(req)

        # Read raw HTTP responses and print them
        reader = threading.Thread(target=read_responses, args=(sock,), daemon=True)
        reader.start()
        reader.join(timeout=30)

        print("\n--- Done reading responses ---")


if __name__ == "__main__":
    main()
