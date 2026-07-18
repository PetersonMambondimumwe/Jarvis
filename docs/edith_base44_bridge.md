# EDITH Base44 Bridge

Jarvis calls EDITH through either the direct Base44 agent API or a Base44
backend function/webhook endpoint.

## Jarvis Config

Add these keys to `config/api_keys.json`:

```json
{
  "base44_api_key": "your-shared-secret",
  "base44_agent_name": "edith",
  "base44_edith_agent_id": "6a00597ec92cb8615f50b66d",
  "base44_edith_agent_api_base": "https://app.base44.com/api/agents/6a00597ec92cb8615f50b66d",
  "base44_edith_chat_url": "https://app.base44.com/superagent/6a00597ec92cb8615f50b66d",
  "base44_timeout_seconds": "30"
}
```

For the direct Base44 agent API, Jarvis sends `base44_api_key` as the exact
`api_key` header and as the `?api_key=...` query parameter. It also sends
`Authorization: Bearer ...`, `X-API-Key`, and `X-Base44-API-Key` for bridge
deployments that accept those conventions.

If the direct Base44 agent API returns `auth_required` for a private app,
create a Base44 backend function and set:

```json
{
  "base44_edith_endpoint": "https://your-app.base44.app/functions/jarvis-edith"
}
```

The backend function should reject requests when the key does not match the
secret configured in Base44.

## Request Shape

Jarvis sends:

```json
{
  "action": "delegate",
  "agent": "edith",
  "agent_name": "edith",
  "task": "The user's task",
  "message": "The user's task",
  "metadata": {
    "orchestrator": "Jarvis-Mark-XLVIII",
    "source": "jarvis",
    "timestamp": "2026-07-17T12:00:00Z",
    "session_memory": {}
  }
}
```

For connectivity checks, `action` is `verify`.

## Response Shape

Return any of these fields as a string:

```json
{
  "result": "EDITH completed the task."
}
```

Jarvis also accepts `reply`, `response`, `message`, or `content`.

## Base44 Function Sketch

Inside a Base44 backend function, create a Base44 client from the request, check
the shared key, create an agent conversation for EDITH, add the user message,
and return EDITH's reply.

```ts
import { createClientFromRequest } from "npm:@base44/sdk";

Deno.serve(async (req) => {
  const expectedKey = Deno.env.get("JARVIS_EDITH_API_KEY");
  const auth = req.headers.get("authorization") || "";
  const apiKey = req.headers.get("x-api-key") || "";
  const supplied = auth.startsWith("Bearer ") ? auth.slice(7) : apiKey;

  if (!expectedKey || supplied !== expectedKey) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = await req.json();
  if (body.action === "verify") {
    return Response.json({ result: "EDITH bridge online." });
  }

  const task = String(body.task || body.message || "").trim();
  if (!task) {
    return Response.json({ error: "Missing task" }, { status: 422 });
  }

  const base44 = createClientFromRequest(req);
  const conversation = await base44.asServiceRole.agents.createConversation({
    agent_name: body.agent_name || "edith",
    metadata: body.metadata || {},
  });

  await base44.asServiceRole.agents.addMessage(conversation, {
    role: "user",
    content: task,
  });

  const updated = await base44.asServiceRole.agents.getConversation(conversation.id);
  const assistantMessage = [...(updated?.messages || [])]
    .reverse()
    .find((message) => message.role === "assistant");

  return Response.json({
    result: assistantMessage?.content || "EDITH received the task.",
    conversation_id: conversation.id,
  });
});
```

Set the secret in Base44:

```bash
base44 secrets set JARVIS_EDITH_API_KEY=your-shared-secret
```
