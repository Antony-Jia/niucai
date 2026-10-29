// Pi 节点的可见 Chrome 工具；仅连接部署者指定的本机 CDP，不经 Kernel 或 relay。
const TIMEOUT = 15000;

export async function browserAction(args, endpoint = process.env.NIUCAI_BROWSER_CDP_URL) {
  const base = new URL(endpoint || "http://127.0.0.1:9222");
  if (!["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)) {
    throw new Error("Browser CDP must use node loopback");
  }
  const response = await fetch(new URL("/json/list", base), { signal: AbortSignal.timeout(TIMEOUT) });
  if (!response.ok) throw new Error(`CDP discovery failed: ${response.status}`);
  const pages = (await response.json()).filter(p => p.type === "page");
  if (args.action === "tabs") return pages.map(({id, title, url}) => ({id, title, url}));
  const page = args.target_id ? pages.find(p => p.id === args.target_id) : pages[0];
  if (!page) throw new Error("No matching visible Chrome tab; use tabs first");
  const wsUrl = new URL(page.webSocketDebuggerUrl);
  wsUrl.hostname = base.hostname;
  wsUrl.port = base.port;
  const socket = new WebSocket(wsUrl);
  let sequence = 0;
  const pending = new Map();
  const fail = error => { for (const item of pending.values()) item.reject(error); pending.clear(); };
  socket.addEventListener("message", event => {
    const value = JSON.parse(event.data);
    const item = pending.get(value.id);
    if (!item) return;
    pending.delete(value.id);
    value.error ? item.reject(new Error(value.error.message)) : item.resolve(value.result);
  });
  socket.addEventListener("close", () => fail(new Error("CDP connection closed")));
  socket.addEventListener("error", () => fail(new Error("CDP connection failed")));
  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, TIMEOUT);
      pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
      socket.send(JSON.stringify({id, method, params}));
    });
  }
  async function evaluate(expression) {
    const value = await call("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (value.exceptionDetails) throw new Error(value.exceptionDetails.text || "Page evaluation failed");
    return value.result.value;
  }
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("CDP connect timeout")), TIMEOUT);
      socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, {once:true});
      socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("CDP connect failed")); }, {once:true});
    });
    if (args.action === "navigate") {
      const url = new URL(args.url);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("Only HTTP(S) navigation is allowed");
      const result = await call("Page.navigate", {url: url.href});
      if (result.errorText) throw new Error(result.errorText);
      // 等待新文档；不使用任意固定长等待。
      for (let i = 0; i < 50; i++) {
        const ready = await evaluate("document.readyState");
        const current = await evaluate("location.href");
        if (current !== "about:blank" && ["interactive", "complete"].includes(ready) && current === url.href) break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      return await evaluate("({url:location.href,title:document.title})");
    }
    if (args.action === "snapshot") {
      return await evaluate(`(() => {
        const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
        const elements = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"]')].filter(visible).slice(0,100);
        return {url:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,12000),elements:elements.map((e,i)=>{
          e.setAttribute('data-niucai-ref',String(i));
          return {selector:'[data-niucai-ref="'+i+'"]',tag:e.tagName.toLowerCase(),text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,200),type:e.getAttribute('type'),href:e.getAttribute('href')};
        })};
      })()`);
    }
    if (args.action === "click" || args.action === "fill") {
      if (!args.selector) throw new Error("selector is required; read snapshot first");
      const selector = JSON.stringify(args.selector);
      if (args.action === "fill") {
        if (typeof args.text !== "string") throw new Error("text is required");
        return await evaluate(`(() => { const e=document.querySelector(${selector}); if(!e)throw Error('Element not found'); if(!['INPUT','TEXTAREA'].includes(e.tagName))throw Error('Element is not a text input'); e.focus(); const setter=Object.getOwnPropertyDescriptor(e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,'value').set; setter.call(e,${JSON.stringify(args.text)}); e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); return {filled:true}; })()`);
      }
      const point = await evaluate(`(() => {const e=document.querySelector(${selector});if(!e)throw Error('Element not found');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();if(!r.width||!r.height)throw Error('Element is hidden');return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
      await call("Input.dispatchMouseEvent", {type:"mousePressed",...point,button:"left",clickCount:1});
      await call("Input.dispatchMouseEvent", {type:"mouseReleased",...point,button:"left",clickCount:1});
      return {clicked:true};
    }
    if (args.action === "press") {
      const keys = {Enter:13,Tab:9,Escape:27,ArrowDown:40,ArrowUp:38,Backspace:8};
      if (!keys[args.key]) throw new Error("Unsupported key");
      const params = {key:args.key,code:args.key,windowsVirtualKeyCode:keys[args.key]};
      await call("Input.dispatchKeyEvent", {type:"keyDown",...params,...(args.key==='Enter'?{text:'\r'}:{})});
      await call("Input.dispatchKeyEvent", {type:"keyUp",...params});
      return {pressed:args.key};
    }
    if (args.action === "screenshot") return await call("Page.captureScreenshot", {format:"png",captureBeyondViewport:false});
    throw new Error("Unknown browser action");
  } finally {
    fail(new Error("CDP request ended"));
    socket.close();
  }
}
