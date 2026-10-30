/**
 * 输入框里的 @助手：只做文本解析，不判断助手是否可用。
 * 助手名只允许字母、数字、下划线、连字符和汉字（与 Kernel 的 BOT_NAME 一致）。
 */
const NAME = "A-Za-z0-9_\\-\\u4e00-\\u9fff";
const ACTIVE = new RegExp(`(^|[^${NAME}])@([${NAME}]{0,60})$`);
const MENTION = new RegExp(`(^|[^${NAME}])@([${NAME}]{1,60})`, "g");

export interface ActiveMention {
  /** “@”在文本中的位置。 */
  start: number;
  query: string;
}

/** 光标前正在输入的 @片段；光标不在 @词里时返回 null。 */
export function activeMention(
  text: string,
  caret: number,
): ActiveMention | null {
  const before = text.slice(0, caret);
  const match = ACTIVE.exec(before);
  if (!match) return null;
  return { start: caret - match[2].length - 1, query: match[2] };
}

/** 用选中的助手名替换正在输入的 @片段，并在后面补一个空格。 */
export function insertMention(
  text: string,
  caret: number,
  mention: ActiveMention,
  name: string,
) {
  const tail = text.slice(caret);
  const insert = `@${name}${tail.startsWith(" ") ? "" : " "}`;
  const next = text.slice(0, mention.start) + insert + tail;
  return { text: next, caret: mention.start + `@${name} `.length };
}

/** 文本里实际提到的助手：只认已存在的名称，按首次出现的顺序去重。 */
export function mentionedBots(text: string, names: string[]): string[] {
  const known = new Set(names);
  const found: string[] = [];
  for (const match of text.matchAll(MENTION))
    if (known.has(match[2]) && !found.includes(match[2])) found.push(match[2]);
  return found;
}
