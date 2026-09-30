import asyncio
import os
import signal


class DisabledAdapter:
    async def execute(self, spec, computer):
        raise RuntimeError("computer adapter is disabled; configure a remote computer first")


class FakeAdapter:
    """Deterministic test adapter. Never use this for actual computer control."""

    async def execute(self, spec, computer):
        if spec["type"] == "browser.snapshot":
            return {"observation": "button e1 Demo", "refs": ["e1"], "url": "https://example.com"}
        return {"ok": True, "adapter": "fake", "type": spec["type"]}


class LocalAdapter:
    """Files/shell in a dedicated workspace; browser over private CDP."""

    def __init__(self, settings):
        self.settings = settings
        self.root = settings.workspace.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.playwright = None
        self.browser = None

    def path(self, name):
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("path must remain inside workspace")
        return path

    async def page(self):
        if self.browser is None or not self.browser.is_connected():
            from playwright.async_api import async_playwright

            if self.playwright is None:
                self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(self.settings.browser_cdp_url)
        if not self.browser.contexts:
            raise RuntimeError("persistent Chromium context is missing")
        context = self.browser.contexts[0]
        return context.pages[0] if context.pages else await context.new_page()

    async def close(self):
        if self.playwright:
            await self.playwright.stop()

    async def execute(self, spec, computer):
        kind = spec["type"]
        if kind == "files.read":
            path = self.path(spec["path"])
            with path.open("r", encoding="utf-8") as f:
                content = f.read(self.settings.tool_result_chars + 1)
            return {
                "content": content[: self.settings.tool_result_chars],
                "truncated": len(content) > self.settings.tool_result_chars,
            }
        if kind == "files.write":
            path = self.path(spec["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(spec["content"])
            return {"path": str(path.relative_to(self.root)), "bytes": path.stat().st_size}
        if kind == "shell.exec":
            if not self.settings.allow_shell:
                raise PermissionError("shell execution disabled")
            # Never expose API credentials through the child environment.
            env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(self.root)}
            process = await asyncio.create_subprocess_exec(
                *spec["argv"],
                cwd=self.root,
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output = bytearray()

            async def drain():
                while chunk := await process.stdout.read(4096):
                    if len(output) < self.settings.tool_result_chars:
                        output.extend(chunk[: self.settings.tool_result_chars - len(output)])
                await process.wait()

            try:
                await asyncio.wait_for(drain(), self.settings.action_timeout - 1)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                raise
            return {"exit_code": process.returncode, "output": output.decode(errors="replace")}
        page = await self.page()
        page.set_default_timeout(int(self.settings.action_timeout * 500))
        if kind == "browser.navigate":
            await page.goto(spec["url"], wait_until="domcontentloaded")
            return {"url": page.url, "refs": []}
        if kind == "browser.snapshot":
            # References are scoped to a persisted snapshot and checked by the gateway.
            data = await page.evaluate("""() => {
              document.querySelectorAll('[data-niucai-ref]')
                .forEach(e => e.removeAttribute('data-niucai-ref'));
              const rows = [...document.querySelectorAll('a,button,input,textarea,select,[role]')]
                .filter(e => e.getClientRects().length).slice(0,150).map((e,i) => {
                  const ref='e'+(i+1); e.setAttribute('data-niucai-ref',ref);
                  return {ref,tag:e.tagName,role:e.getAttribute('role'),
                    name:(e.getAttribute('aria-label') || e.innerText ||
                          e.getAttribute('placeholder') || '').slice(0,300)};
                });
              return {url:location.href,title:document.title,refs:rows.map(r=>r.ref),elements:rows,
                      text:document.body.innerText.slice(0,8000)};
            }""")
            return data
        if kind in {"browser.click", "browser.fill"}:
            locator = page.locator(f'[data-niucai-ref="{spec["ref"]}"]')
            if await locator.count() != 1:
                raise ValueError("stale reference; request a new snapshot")
            if kind == "browser.click":
                await locator.click(button=spec["button"])
            else:
                await locator.fill(spec["text"])
            return {"url": page.url, "refs": []}
        if kind == "browser.scroll":
            await page.mouse.wheel(spec["x"], spec["y"])
            return {"url": page.url, "refs": []}
        if kind == "browser.screenshot":
            from niucai.storage.db import uid

            path = self.path(f"artifacts/{uid()}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path))
            return {"path": str(path.relative_to(self.root)), "media_type": "image/png"}
        raise ValueError("unsupported action")
