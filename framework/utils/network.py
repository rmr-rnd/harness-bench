import os
import socket


def get_mcp_host() -> str:
    """Return the host address agent containers should use to reach MCP servers.

    - Outside Docker (no HARNESS_DOCKER_NETWORK): host.docker.internal
    - Inside Docker: own IP on the bridge network, so spawned agent containers
      can reach MCP servers running in this process regardless of container name.
    """
    if not os.environ.get("HARNESS_DOCKER_NETWORK", ""):
        return os.environ.get("HARNESS_MCP_HOST", "host.docker.internal")

    explicit = os.environ.get("HARNESS_MCP_HOST", "")
    if explicit and explicit not in ("framework",):
        return explicit

    # Detect own IP visible on the Docker network
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()
