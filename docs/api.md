# REST API and webhooks

Open **Panel settings → Integrations** on your Roboport installation. The **API keys** section issues keys for tools that call the panel. **Webhooks** let the panel send events to another service.

## Make a request

Create a key with the `read` permission. Copy the key when the panel displays it and store it in your secret manager. With that key in the `ROBOPORT_API_KEY` environment variable and your panel's HTTPS address in `ROBOPORT_URL`, list its Factorio servers:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${ROBOPORT_API_KEY}" \
  "${ROBOPORT_URL}/api/v1/servers"
```

The `X-API-Key` header also accepts the key. Grant only the permissions the integration needs. Each scope is independent.

| Scope | Typical use |
| --- | --- |
| `read` | List servers, see players and read metrics. |
| `command` | Send an RCON command to a running server. |
| `control` | Start or stop a server, save a world, or manage restart scheduled tasks. |
| `admin` | Create servers, manage files and mods, or configure webhooks. |

The running panel serves an interactive reference at `/api/v1/docs` (Swagger UI), a reading view at `/api/v1/redoc`, and the machine-readable schema at `/api/v1/openapi.json`. Uptime monitors can call `/api/v1/health` and `/api/v1/panel/version` without a key. The latter returns the installed panel version.

Use `control` to manage scheduled restarts. Creating or editing a scheduled RCON command, including re-enabling it, requires both `control` and `command`.

## Receive an event

In **Webhooks**, add your receiver's HTTPS URL and select events such as `server.started` or `player.joined`. Copy the signing secret and click **Test** to send a `webhook.ping`.

The receiver can verify `X-Roboport-Signature` against an HMAC-SHA256 of the original request body using the signing secret. The header includes the `sha256=` prefix. Read the event name from `X-Roboport-Event` and use `X-Roboport-Delivery-Id` to recognize repeated deliveries.

[Operator guide](../deploy/README.md) · [Project home](../README.md)
