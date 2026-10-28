# RFC 6455 WebSocket Protocol Implementation from Scratch

A low-level implementation of the WebSocket Protocol (RFC 6455) built over raw TCP sockets without high-level networking abstractions or third-party WebSocket libraries. Designed to explore binary wire framing, bitwise header parsing, payload masking/unmasking, and stateful full-duplex communication.

---

## Overview

The WebSocket protocol provides full-duplex communication channels over a single TCP connection, starting with an HTTP-based handshake and upgrading to a binary framing layer. 

While modern frameworks abstract this transport layer, this project implements the core protocol mechanics from scratch:
* Processing HTTP `Upgrade: websocket` negotiation headers.
* Computing cryptographically valid handshake response signatures via SHA-1 and Base64 encoding.
* Bit-level parsing of incoming WebSocket frames across variable payload boundaries.
* XOR-based payload de-masking algorithms for client-to-server data frames.
* Handling connection lifecycle states, including Ping/Pong heartbeat frames and close handshakes.

---

## Architectural Breakdown

### 1. HTTP Handshake & Connection Upgrade
According to RFC 6455, connection initiation requires a client handshake containing a 16-byte nonce (`Sec-WebSocket-Key`). The server must:
1. Extract the `Sec-WebSocket-Key` string from incoming HTTP request headers.
2. Concatenate the globally unique GUID: `258EAFA5-E914-47DA-95CA-C5AB0DC85B11`.
3. Compute the SHA-1 hash of the concatenated string.
4. Encode the 20-byte hash using Base64.
5. Return an `HTTP/1.1 101 Switching Protocols` response with the resulting `Sec-WebSocket-Accept` header.

### 2. Binary Framing & Bitwise Parsing
Once upgraded, all traffic follows the RFC 6455 framing format. The parser inspects raw byte streams to extract:
* **FIN bit (bit 0)**: Indicates whether the frame is the final fragment in a message.
* **RSVP bits (bits 1-3)**: Reserved extension bits (must be 0 unless negotiated).
* **Opcode (bits 4-7)**:
  * `0x1`: Text Frame (UTF-8 payload)
  * `0x2`: Binary Frame
  * `0x8`: Connection Close
  * `0x9`: Ping
  * `0xA`: Pong
* **MASK bit (bit 8)**: Enforces that all client-to-server frames are masked (rejects unmasked frames with protocol error).
* **Payload Length Decoder**:
  * Values $\le 125$: 7-bit direct payload length.
  * Value $126$: Extended 16-bit unsigned integer (next 2 bytes).
  * Value $127$: Extended 64-bit unsigned integer (next 8 bytes).

### 3. Payload De-Masking Algorithm
Client-to-server payloads are obfuscated with a 4-byte masking key. The original data is recovered via a cyclic XOR operation:

$$\text{OriginalByte}[i] = \text{MaskedByte}[i] \oplus \text{MaskingKey}[i \pmod 4]$$

This bitwise logic is optimized for throughput across streaming buffers before forwarding payloads to application handlers.

### 4. Control Frame Dispatching
* **Ping / Pong (`0x9` / `0xA`)**: Implements automatic heartbeat response to keep persistent TCP connections alive and detect dead peers.
* **Close (`0x8`)**: Gracefully terminates connections via a bi-directional close frame exchange with status codes (e.g., 1000 Normal Closure).

---

## Project Structure

```text
.
├── src/
│   ├── server.cpp / server.py      # TCP server socket listener & connection loop
│   ├── handshake.hpp / handshake   # HTTP header parsing & SHA-1/Base64 accept key generator
│   ├── frame_parser.hpp / parser   # Bitwise frame decoding (FIN, Opcode, Length, Mask)
│   └── websocket.hpp / handler     # State machine & payload dispatching logic
├── tests/                          # Automated handshake and framing test cases
├── Makefile / CMakeLists.txt
└── README.md
