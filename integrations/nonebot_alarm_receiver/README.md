# NoneBot OpenLab alarm receiver

Copy this folder into the NoneBot2 project's plugin directory, rename it if
needed, and configure the four values shown in `.env.example`.

The receiver deliberately ignores caller-selected QQ numbers:

- `warning` is sent only to `alarm_tester_qqs`;
- `error` is sent to the union of `alarm_admin_qqs` and
  `alarm_tester_qqs`.

`alarm_token` is mandatory. If it is missing, the endpoint fails closed with
HTTP 503. The sender includes a stable `event_id`; successful recipients are
remembered for `alarm_delivery_ttl_seconds`, so HTTP retries do not normally
duplicate QQ messages. Partially failed deliveries retry only the recipients
that have not succeeded yet.

Example request:

```text
POST /alarm/report
X-Token: <same secret as the OpenLab sender>
Content-Type: application/json

{"event_id":"0123456789abcdef","level":"warning","message":"test"}
```

Use HTTPS when the receiver is not on the same machine. Do not commit real
tokens or QQ lists.
