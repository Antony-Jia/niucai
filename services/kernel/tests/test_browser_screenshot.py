import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from niucai.actions.adapters import LocalAdapter
from niucai.config import Settings


def adapter_with_page(tmp_path, outcomes):
    adapter = LocalAdapter(Settings(_env_file=None, workspace=tmp_path))
    page = AsyncMock()
    page.set_default_timeout = lambda value: None
    page.screenshot.side_effect = outcomes
    adapter.page = AsyncMock(return_value=page)
    return adapter, page


async def test_screenshot_success_does_not_change_foreground(tmp_path):
    adapter, page = adapter_with_page(tmp_path, [None])
    result = await adapter.execute({"type": "browser.screenshot"}, None)
    assert result["media_type"] == "image/png"
    assert Path(result["path"]).parts[0] == "artifacts"
    page.bring_to_front.assert_not_awaited()
    assert page.screenshot.await_args.kwargs["timeout"] == 8000


async def test_background_screenshot_retries_same_path_after_activation(tmp_path):
    adapter, page = adapter_with_page(tmp_path, [TimeoutError("no frame"), None])
    await adapter.execute({"type": "browser.screenshot"}, None)
    page.bring_to_front.assert_awaited_once()
    first, second = page.screenshot.await_args_list
    assert first.kwargs["path"] == second.kwargs["path"]
    assert second.kwargs["timeout"] == 20000
    assert [call[0] for call in page.mock_calls] == [
        "screenshot", "bring_to_front", "screenshot"
    ]


async def test_second_screenshot_failure_propagates_without_more_retries(tmp_path):
    adapter, page = adapter_with_page(
        tmp_path, [TimeoutError("background"), TimeoutError("still stalled")]
    )
    with pytest.raises(TimeoutError, match="still stalled"):
        await adapter.execute({"type": "browser.screenshot"}, None)
    assert page.screenshot.await_count == 2


async def test_screenshot_cancellation_does_not_activate_or_retry(tmp_path):
    adapter, page = adapter_with_page(tmp_path, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await adapter.execute({"type": "browser.screenshot"}, None)
    page.bring_to_front.assert_not_awaited()
    assert page.screenshot.await_count == 1
