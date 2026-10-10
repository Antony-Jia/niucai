/** Transport adapter only. Model policy, credentials and provider requests live in Python. */
import { createProvider } from "@earendil-works/pi-ai/models";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai/utils/event-stream";
import {
  collapseSystemMessages,
  getCurrentSystemPrompt,
  getCurrentTools,
} from "@earendil-works/pi-ai/utils/transcript";
import type {
  AssistantMessage,
  Model,
  TranscriptContext,
  StreamFunction,
  Message,
} from "@earendil-works/pi-ai";

export type Rpc = (
  method: string,
  params: Record<string, unknown>,
) => Promise<any>;

function content(blocks: any): any {
  if (typeof blocks === "string") return blocks;
  const converted = blocks.flatMap((b: any) => {
    if (b.type === "text") return [{ type: "text", text: b.text }];
    if (b.type === "image")
      return [
        {
          type: "image_url",
          image_url: { url: `data:${b.mimeType};base64,${b.data}` },
        },
      ];
    return [];
  });
  return converted.every((b: any) => b.type === "text")
    ? converted.map((b: any) => b.text).join("\n")
    : converted;
}

export function toGateway(context: TranscriptContext) {
  const collapsed = collapseSystemMessages(context);
  const messages: any[] = [];
  const inputIds: string[] = [];
  const prompt = getCurrentSystemPrompt(context.messages);
  if (prompt) messages.push({ role: "system", content: prompt });
  for (const m of collapsed.messages) {
    if (m.role === "system") continue;
    if (m.role === "user") {
      let value = content(m.content);
      if (typeof value === "string") {
        const marker = value.match(/^\[niucai\.input:([a-zA-Z0-9-]+)\]\n/);
        if (marker?.[1]) {
          inputIds.push(marker[1]);
          value = value.slice(marker[0].length);
        }
      }
      messages.push({ role: "user", content: value });
    }
    if (m.role === "assistant") {
      const calls = m.content.filter((b) => b.type === "toolCall");
      const thinking = m.content
        .filter((b) => b.type === "thinking")
        .map((b) => b.thinking)
        .join("\n");
      messages.push({
        role: "assistant",
        content: content(m.content),
        ...(thinking ? { reasoning_content: thinking } : {}),
        ...(calls.length
          ? {
              tool_calls: calls.map((c) => ({
                id: c.id,
                type: "function",
                function: {
                  name: c.name,
                  arguments: JSON.stringify(c.arguments),
                },
              })),
            }
          : {}),
      });
    }
    if (m.role === "toolResult")
      messages.push({
        role: "tool",
        tool_call_id: m.toolCallId,
        content: content(m.content),
      });
  }
  const tools = getCurrentTools(context.messages).map((t) => ({
    type: "function",
    function: {
      name: t.name,
      description: t.description,
      parameters: t.parameters,
    },
  }));
  return {
    messages,
    ...(tools.length ? { tools } : {}),
    ...(inputIds.length ? { inputIds } : {}),
  };
}

export function kernelProvider(
  rpc: Rpc,
  contextWindow: number,
  maxTokens = 4096,
) {
  const model: Model<"openai-completions"> = {
    id: "executor",
    name: "Kernel executor role",
    api: "openai-completions",
    provider: "niucai",
    baseUrl: "http://kernel.invalid",
    input: ["text", "image"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow,
    maxTokens,
  };
  const stream: StreamFunction<"openai-completions"> = (selected, context) => {
    const events = createAssistantMessageEventStream();
    const partial: AssistantMessage = {
      role: "assistant",
      content: [],
      api: selected.api,
      provider: selected.provider,
      model: selected.id,
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason: "stop",
      timestamp: Date.now(),
    };
    events.push({ type: "start", partial });
    void (async () => {
      try {
        const data = await rpc("model.chat", {
          role: selected.id,
          ...toGateway(context),
        });
        const choice = data.choices[0];
        const message = choice.message;
        if (message.reasoning_content)
          partial.content.push({
            type: "thinking",
            thinking: message.reasoning_content,
          });
        if (typeof message.content === "string" && message.content)
          partial.content.push({ type: "text", text: message.content });
        else if (Array.isArray(message.content))
          partial.content.push(
            ...message.content.filter((b: any) => b.type === "text"),
          );
        for (const call of message.tool_calls ?? [])
          partial.content.push({
            type: "toolCall",
            id: call.id,
            name: call.function.name,
            arguments: JSON.parse(call.function.arguments),
          });
        const usage = data.usage ?? {};
        partial.usage = {
          input: Math.max(
            0,
            (usage.prompt_tokens ?? 0) -
              (usage.prompt_tokens_details?.cached_tokens ?? 0),
          ),
          output: usage.completion_tokens ?? 0,
          cacheRead: usage.prompt_tokens_details?.cached_tokens ?? 0,
          cacheWrite: 0,
          totalTokens: usage.total_tokens ?? 0,
          cost: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
            total: typeof usage.cost === "number" ? usage.cost : 0,
          },
        };
        partial.stopReason = message.tool_calls?.length
          ? "toolUse"
          : choice.finish_reason === "length"
            ? "length"
            : "stop";
        events.push({
          type: "done",
          reason: partial.stopReason,
          message: partial,
        });
        events.end(partial);
      } catch (error) {
        partial.stopReason = "error";
        partial.errorMessage =
          error instanceof Error ? error.message : String(error);
        events.push({ type: "error", reason: "error", error: partial });
        events.end(partial);
      }
    })();
    return events;
  };
  return createProvider({
    id: "niucai",
    auth: {
      apiKey: { name: "Kernel IPC", resolve: async () => ({ auth: {} }) },
    },
    models: [model],
    api: { stream, streamSimple: stream },
  });
}
