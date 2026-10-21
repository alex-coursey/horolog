# ChatGPT and MCP integration

The [MCP bridge](../services/mcp/) exposes Horolog tasks, fixed commitments,
availability, and schedule summaries to ChatGPT or another Streamable HTTP MCP
client. It runs beside the Horolog API; it does not need to run on the same host.
The bridge is a separate process and is not started by `docker compose up`.

## Set up the bridge

1. Run a compatible Horolog API and make it reachable from the bridge. Keep the
   API on a private network because Horolog has no built-in user authentication.
2. Provide an OIDC authorization server that issues RS256 JWT access tokens and
   publishes a JWKS endpoint. The bridge checks the token signature, issuer,
   audience, expiration, and a single allowed subject. Configure the provider's
   OAuth authorization-code flow and discovery metadata for the MCP client.
3. Set the variables below in your service manager or shell. The sample at
   [`services/mcp/.env.example`](../services/mcp/.env.example) contains placeholders,
   not credentials. Keep your real environment file out of Git.
4. Install and run the bridge from the repository root:

   ```sh
   python3 -m venv services/mcp/.venv
   services/mcp/.venv/bin/pip install -r services/mcp/requirements.txt
   cd services/mcp
   .venv/bin/python server.py
   ```

| Variable | Purpose |
| --- | --- |
| `HOROLOG_API_URL` | Base URL of the private Horolog API; defaults to `http://127.0.0.1:8000`. |
| `HOROLOG_MCP_HOST`, `HOROLOG_MCP_PORT` | Bind address and port; default to `127.0.0.1:8100`. |
| `HOROLOG_MCP_RESOURCE_URL` | Required external URL, such as `https://mcp.example.com/mcp`. It identifies this MCP server and supplies its allowed host and origin. |
| `HOROLOG_OAUTH_ISSUER` | Exact issuer URL in the access token and provider metadata. |
| `HOROLOG_OAUTH_JWKS_URL` | JWKS URL for verifying access-token signatures. |
| `HOROLOG_OAUTH_CLIENT_ID` | OAuth client ID, returned with the verified token to the MCP SDK. |
| `HOROLOG_OAUTH_SUBJECT` | Only this token subject can use the bridge. |
| `HOROLOG_OAUTH_AUDIENCE` | Optional expected token audience; defaults to `HOROLOG_MCP_RESOURCE_URL`. Set it explicitly if your provider issues a different audience. |

Put HTTPS in front of the bridge and forward `/mcp` to its local port. Use the
same public URL in `HOROLOG_MCP_RESOURCE_URL`. Keep the bridge bound to loopback
when using a reverse proxy on the same host. A remote MCP client must be able to
reach the HTTPS endpoint; Tailscale-only DNS is insufficient for a cloud client
unless you use a supported secure tunnel. Follow the [official OpenAI connection
guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) to add the
custom MCP server in ChatGPT and configure OAuth. The [OpenAI authentication
guide](https://developers.openai.com/plugins/build/auth) covers provider discovery,
PKCE, redirect URIs, and protected-resource metadata.

The bridge grants its authorized subject access to all its tools, including
delete and edit operations. Do not expose the Horolog API directly to the
internet; the bridge's OAuth protection does not protect that separate API.

## Tool behavior

The bridge lists, retrieves, creates, edits, completes, uncompletes, and deletes
tasks. It also manages fixed calendar blocks, skips or restores fixed weekly
sessions, and reports availability, the plan, and a schedule summary.

`update_task(task_id, changes)` sends a partial PATCH. Omitted fields remain
unchanged; null clears nullable fields. `tags` replaces the list, while
`add_tags` and `remove_tags` modify it. Null color restores the priority default.
The API validates the merged task, including windows, date bounds, attendee
constraints, completion, tags, and recurrence. Existing PUT replacement remains
available through the REST API.

Task dates may be ISO datetimes with offsets or floating local datetimes, which
use Horolog's configured timezone. The solver uses 15-minute wall-clock slots.
Deadlines are soft targets: the plan reports late blocks separately from unmet
demand. Already started placements remain stable when constraints still permit.

For fixed weekly sessions, `allowed_weekdays` uses Monday=0 through Sunday=6.
When creating one, `duration_minutes` is per session, not the full weekly total.
For example, 45 minutes on Monday and Sunday becomes 90 minutes per week with
45-minute chunks. Editing weekdays alone preserves session length;
`session_minutes` changes it. `skip_task_session` subtracts one supported fixed
weekly session from its period; `restore_task_session` undoes the skip. General
weekly time budgets do not support per-session skipping.

`create_calendar_block` accepts ISO start and end times, or an `all_day_date`
such as `2030-01-15`. An optional exclusive `end_date` spans multiple days.
For example, `title="Conference day", all_day_date="2030-01-15"` reserves that
day. Blocks are Horolog commitments; they do not create events in Google or
Outlook. `list_calendar_blocks` returns IDs for edits or deletion.

`get_availability(mode="bookable")` allows movable tasks to shift around a
booking. `mode="idle"` excludes scheduled tasks too. Both respect working
hours, fixed commitments, and the current time.

Supply a unique `request_id` when calling `create_task` or
`create_calendar_block`, and reuse it for retries. The bridge forwards it as an
`Idempotency-Key`. Reusing a key with different input returns 409. A deliberate
new item, even with identical content, needs a new key.

## Verify

Run the bridge's offline tests from the repository root:

```sh
services/mcp/.venv/bin/python -m unittest discover -s services/mcp -p test_tools.py -v
```

These tests use mocked HTTP requests and example OAuth settings. They do not
contact your identity provider or modify Horolog data. Test the deployed MCP
endpoint with an MCP client after configuring HTTPS and OAuth.
