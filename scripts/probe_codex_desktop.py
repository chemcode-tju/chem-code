"""Read-only probe of the running desktop IPC router; never starts a turn."""
# ruff: noqa: E402

# Configuration
thread_id = "01a1ea3b-5384-7213-9c54-d9e5274f441c"
socket_path = "~/.codex/ipc/ipc.sock"

import json
import socket
import struct
from pathlib import Path
from uuid import uuid4


def receive_exact(connection: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        part = connection.recv(count - len(chunks))
        if not part:
            raise ConnectionError("Desktop IPC closed")
        chunks.extend(part)
    return bytes(chunks)


def request(connection: socket.socket, client: str, method: str, params: dict) -> dict:
    identifier = str(uuid4())
    message = {
        "type": "request",
        "requestId": identifier,
        "sourceClientId": client,
        "version": 1,
        "method": method,
        "params": params,
    }
    payload = json.dumps(message).encode()
    connection.sendall(struct.pack("<I", len(payload)) + payload)
    while True:
        size = struct.unpack("<I", receive_exact(connection, 4))[0]
        if size > 16 * 1024 * 1024:
            raise ValueError("Unexpected IPC frame size")
        result = json.loads(receive_exact(connection, size))
        if result.get("type") == "client-discovery-request":
            reply = json.dumps(
                {
                    "type": "client-discovery-response",
                    "requestId": result["requestId"],
                    "response": {"canHandle": False},
                }
            ).encode()
            connection.sendall(struct.pack("<I", len(reply)) + reply)
        if result.get("type") == "response" and result.get("requestId") == identifier:
            return result


def main() -> None:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(15)
        connection.connect(str(Path(socket_path).expanduser()))
        initialized = request(
            connection, "initializing-client", "initialize", {"clientType": "chem-research-probe"}
        )
        print(json.dumps({"initialize": initialized}, ensure_ascii=False))
        client = initialized["result"]["clientId"]
        owner = request(
            connection,
            client,
            "thread-owner-discovery",
            {"hostId": "local", "conversationId": thread_id},
        )
        print(json.dumps({"owner": owner}, ensure_ascii=False))


if __name__ == "__main__":
    main()
