# Horolog MCP bridge

A Streamable HTTP MCP server that exposes Horolog tasks and schedules to
ChatGPT or another compatible MCP client. See [the setup guide](../../docs/chatgpt-mcp.md)
for configuration, security requirements, and tool behavior.

The bridge reads the Horolog API at `HOROLOG_API_URL` and validates RS256 JWT
access tokens from a configured OIDC provider. It does not hold an API key for
Horolog; keep the Horolog API private to the bridge and trusted local clients.

Run offline tests with:

```sh
python -m unittest discover -s services/mcp -p test_tools.py -v
```
