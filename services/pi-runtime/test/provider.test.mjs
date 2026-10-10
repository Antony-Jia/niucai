import test from "node:test";
import assert from "node:assert/strict";
import { createModels } from "@earendil-works/pi-ai/models";
import { normalizeContext } from "@earendil-works/pi-ai/utils/transcript";
import { toGateway, kernelProvider } from "../dist/provider.js";

const assistant = (content) => ({
  role: "assistant",
  content,
  api: "openai-completions",
  provider: "niucai",
  model: "executor",
  stopReason: "toolUse",
  timestamp: 1,
  usage: {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  },
});

test("round-trip transcript preserves reasoning, tool arguments, result and image", () => {
  const context = normalizeContext({
    systemPrompt: "Kernel policy",
    tools: [
      {
        name: "execute_action",
        description: "Gate actions",
        parameters: { type: "object" },
      },
    ],
    messages: [
      {
        role: "user",
        content: [{ type: "image", mimeType: "image/png", data: "abc" }],
        timestamp: 1,
      },
      assistant([
        { type: "thinking", thinking: "Refresh observation" },
        {
          type: "toolCall",
          id: "same-id",
          name: "execute_action",
          arguments: { spec: { type: "browser.snapshot" } },
        },
      ]),
      {
        role: "toolResult",
        toolCallId: "same-id",
        toolName: "execute_action",
        content: [{ type: "text", text: '{"status":"FAILED"}' }],
        isError: true,
        timestamp: 1,
      },
    ],
  });
  const result = toGateway(context);
  assert.equal(result.messages[0].content, "Kernel policy");
  assert.equal(
    result.messages[1].content[0].image_url.url,
    "data:image/png;base64,abc",
  );
  assert.equal(result.messages[2].reasoning_content, "Refresh observation");
  assert.deepEqual(
    JSON.parse(result.messages[2].tool_calls[0].function.arguments),
    { spec: { type: "browser.snapshot" } },
  );
  assert.equal(result.messages[3].tool_call_id, "same-id");
  assert.equal(result.messages[3].content, '{"status":"FAILED"}');
  assert.equal(result.tools[0].function.name, "execute_action");
});

test("only inputs actually placed in the transcript contribute to the Kernel revision", () => {
  const context = normalizeContext({
    messages: [
      {
        role: "user",
        content: "[niucai.input:message-one]\nOriginal request",
        timestamp: 1,
      },
      assistant([{ type: "text", text: "working" }]),
    ],
  });
  const result = toGateway(context);
  assert.deepEqual(result.inputIds, ["message-one"]);
  assert.equal(result.messages[0].content, "Original request");
  assert.ok(!JSON.stringify(result.messages).includes("niucai.input"));
});

test("provider calls symbolic Kernel role and records usage without provider credentials", async () => {
  const models = createModels();
  models.setProvider(
    kernelProvider(async (method, params) => {
      assert.equal(method, "model.chat");
      assert.equal(params.role, "executor");
      return {
        choices: [{ message: { content: "回答" }, finish_reason: "stop" }],
        usage: {
          prompt_tokens: 10,
          completion_tokens: 2,
          total_tokens: 12,
          cost: 0.002,
        },
      };
    }, 64000),
  );
  const stream = models.streamSimple(models.getModel("niucai", "executor"), {
    messages: [],
  });
  const events = [];
  for await (const event of stream) events.push(event.type);
  const message = await stream.result();
  assert.deepEqual(events, ["start", "done"]);
  assert.equal(message.content[0].text, "回答");
  assert.equal(message.usage.totalTokens, 12);
  assert.equal(message.usage.cost.total, 0.002);
});

test("invalid provider tool JSON becomes a failed generation, never a computer operation", async () => {
  const models = createModels();
  models.setProvider(
    kernelProvider(
      async () => ({
        choices: [
          {
            message: {
              tool_calls: [
                {
                  id: "broken",
                  function: { name: "execute_action", arguments: "{broken" },
                },
              ],
            },
          },
        ],
      }),
      64000,
    ),
  );
  const stream = models.streamSimple(models.getModel("niucai", "executor"), {
    messages: [],
  });
  const message = await stream.result();
  assert.equal(message.stopReason, "error");
  assert.ok(message.errorMessage);
});
