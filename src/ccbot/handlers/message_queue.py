"""Topic-aware message queue management for ordered Telegram delivery.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue/worker for a specific topic
  - Message queue worker: Background task processing one topic queue
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter

from ..html_converter import convert_markdown, strip_sentinels
from ..session import session_manager
from ..terminal_parser import extract_active_interactive_content, parse_status_line
from ..tmux_manager import tmux_manager
from .interactive_ui import (
    _build_interactive_keyboard,
    clear_interactive_tracking,
    set_interactive_mode,
    set_interactive_msg_id,
)
from .message_sender import NO_LINK_PREVIEW, PARSE_MODE, send_photo, send_with_fallback

logger = logging.getLogger(__name__)

# HTML tags that indicate text is already converted
_HTML_TAGS = ("<pre>", "<code>", "<b>", "<i>", "<a ", "<blockquote", "<u>", "<s>")

def _is_already_html(text: str) -> bool:
    """Check if text already contains Telegram HTML formatting."""
    return any(tag in text for tag in _HTML_TAGS)


def _ensure_html(text: str) -> str:
    """Convert to HTML only if not already converted."""
    if _is_already_html(text):
        return text
    return convert_markdown(text)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal[
        "content",
        "status_update",
        "status_clear",
        "pane_probe",
        "interactive_probe",  # Backward-compatible alias for pane_probe
        "interactive_clear",
    ]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    source: str | None = None  # Probe source for diagnostics (poller/callback/tool_use)
    allow_status: bool = True  # For pane_probe: whether statusline sync is allowed
    completion_fut: asyncio.Future[None] | None = None  # Strict delivery ack


QueueKey: TypeAlias = tuple[int, int]  # (user_id, thread_id_or_0)

# Per-topic message queues and worker tasks
_message_queues: dict[QueueKey, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[QueueKey, asyncio.Task[None]] = {}
_queue_locks: dict[QueueKey, asyncio.Lock] = {}  # Protect drain/refill operations

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Interactive UI render tracking:
# (user_id, thread_id_or_0) -> (message_id, window_id, fingerprint)
_interactive_render_state: dict[tuple[int, int], tuple[int, str, str]] = {}

# Coalescing key for queued pane probes:
# (user_id, thread_id_or_0, window_id)
_pane_probe_pending: set[tuple[int, int, str]] = set()
# Backward-compatible alias name.
_interactive_probe_pending = _pane_probe_pending

# Flood control: queue key -> monotonic time when ban expires
_flood_until: dict[QueueKey, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10

# Lightweight debug counters for observability.
_debug_counters: dict[str, int] = {
    "probe_enqueued": 0,
    "probe_coalesced": 0,
    "interactive_noop": 0,
    "interactive_send": 0,
    "interactive_edit": 0,
    "duplicate_suppressed": 0,
}


def _inc_counter(name: str) -> None:
    _debug_counters[name] = _debug_counters.get(name, 0) + 1


def _queue_key(user_id: int, thread_id: int | None = None) -> QueueKey:
    return (user_id, thread_id or 0)


def get_message_queue(
    user_id: int,
    thread_id: int | None = None,
) -> asyncio.Queue[MessageTask] | None:
    """Get queue for a specific topic (if it exists)."""
    return _message_queues.get(_queue_key(user_id, thread_id))


def get_or_create_queue(
    bot: Bot,
    user_id: int,
    thread_id: int | None = None,
) -> asyncio.Queue[MessageTask]:
    """Get or create queue and worker for a specific topic."""
    key = _queue_key(user_id, thread_id)
    if key not in _message_queues:
        _message_queues[key] = asyncio.Queue()
        _queue_locks[key] = asyncio.Lock()
        _queue_workers[key] = asyncio.create_task(_message_queue_worker(bot, key))
    return _message_queues[key]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if base.thread_id != candidate.thread_id:
        return False
    if candidate.task_type != "content":
        return False
    # Strict delivery tasks must preserve one-task-per-event acknowledgements.
    if base.completion_fut is not None or candidate.completion_fut is not None:
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
        ),
        merge_count,
    )


async def _message_queue_worker(bot: Bot, key: QueueKey) -> None:
    """Process message tasks for one topic sequentially."""
    user_id, tid = key
    queue = _message_queues[key]
    lock = _queue_locks[key]
    logger.info(
        "Message queue worker started (user=%d, thread=%s)",
        user_id,
        tid,
    )

    while True:
        try:
            task = await queue.get()
            task_error: Exception | None = None
            try:
                # Flood control: drop status, wait for content
                flood_end = _flood_until.get(key, 0)
                if flood_end > 0:
                    remaining = flood_end - time.monotonic()
                    if remaining > 0:
                        if task.task_type != "content":
                            # Status is ephemeral — safe to drop
                            continue
                        # Content is actual Claude output — wait then send
                        logger.debug(
                            "Flood controlled: waiting %.0fs for content (user %d)",
                            remaining,
                            user_id,
                        )
                        await asyncio.sleep(remaining)
                    # Ban expired
                    _flood_until.pop(key, None)
                    logger.info(
                        "Flood control lifted (user=%d, thread=%s)",
                        user_id,
                        tid,
                    )

                logger.debug(
                    "Queue task start: task_type=%s source=%s user=%d thread=%s window=%s qsize=%d",
                    task.task_type,
                    task.source or "",
                    user_id,
                    tid,
                    task.window_id or "",
                    queue.qsize(),
                )

                if task.task_type == "content":
                    # Try to merge consecutive content tasks
                    merged_task, merge_count = await _merge_content_tasks(
                        queue, task, lock
                    )
                    if merge_count > 0:
                        logger.debug(f"Merged {merge_count} tasks for user {user_id}")
                        # Mark merged tasks as done
                        for _ in range(merge_count):
                            queue.task_done()
                    await _process_content_task(bot, user_id, merged_task)
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(bot, user_id, task.thread_id or 0)
                elif task.task_type in ("pane_probe", "interactive_probe"):
                    try:
                        await _process_pane_probe_task(bot, user_id, task)
                    finally:
                        task_tid = task.thread_id or 0
                        wid = task.window_id or ""
                        if wid:
                            _pane_probe_pending.discard((user_id, task_tid, wid))
                elif task.task_type == "interactive_clear":
                    await _clear_interactive_render(bot, user_id, task.thread_id or 0)
            except RetryAfter as e:
                task_error = e
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                    _flood_until[key] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for user %d thread %s: retry_after=%ds, "
                        "pausing queue until ban expires",
                        user_id,
                        tid,
                        retry_secs,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d thread %s: waiting %ds",
                        user_id,
                        tid,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                task_error = e
                logger.error(
                    "Error processing message task (user=%d, thread=%s): %s",
                    user_id,
                    tid,
                    e,
                )
            finally:
                if task.completion_fut and not task.completion_fut.done():
                    if task_error is None:
                        task.completion_fut.set_result(None)
                    else:
                        task.completion_fut.set_exception(task_error)
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(
                "Message queue worker cancelled (user=%d, thread=%s)",
                user_id,
                tid,
            )
            break
        except Exception as e:
            logger.error(
                "Unexpected error in queue worker (user=%d, thread=%s): %s",
                user_id,
                tid,
                e,
            )


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


def _interactive_fingerprint(window_id: str, ui_name: str, text: str) -> str:
    """Build a stable fingerprint for interactive UI deduplication."""
    payload = f"{window_id}\n{ui_name}\n{text.strip()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def _clear_interactive_render(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
) -> None:
    """Delete currently tracked interactive UI message (if any)."""
    ikey = (user_id, thread_id_or_0)
    state = _interactive_render_state.pop(ikey, None)
    if not state:
        clear_interactive_tracking(user_id, thread_id_or_0 or None)
        return

    msg_id, _, _ = state
    chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass
    clear_interactive_tracking(user_id, thread_id_or_0 or None)


async def _process_pane_probe_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> bool:
    """Synchronize interactive UI/status from a single pane capture.

    Returns True when an interactive UI is visible after processing.
    """
    tid = task.thread_id or 0
    thread_id = task.thread_id
    wid = task.window_id or ""
    source = task.source or "unknown"
    allow_status = task.allow_status
    if not wid:
        return False

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await _clear_interactive_render(bot, user_id, tid)
        if allow_status:
            await _do_clear_status_message(bot, user_id, tid)
        logger.debug(
            "Interactive probe clear: source=%s user=%d thread=%s window=%s reason=window_missing",
            source,
            user_id,
            thread_id,
            wid,
        )
        return False

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return False

    content = extract_active_interactive_content(pane_text)
    if not content:
        await _clear_interactive_render(bot, user_id, tid)
        if allow_status:
            status_line = parse_status_line(pane_text)
            if status_line:
                await _process_status_update_task(
                    bot,
                    user_id,
                    MessageTask(
                        task_type="status_update",
                        text=status_line,
                        window_id=wid,
                        thread_id=thread_id,
                        source=source,
                    ),
                )
        logger.debug(
            "Interactive probe clear: source=%s user=%d thread=%s window=%s reason=ui_absent",
            source,
            user_id,
            thread_id,
            wid,
        )
        return False

    text = content.content.strip()
    fp = _interactive_fingerprint(wid, content.name, text)
    ikey = (user_id, tid)
    current = _interactive_render_state.get(ikey)

    if current and current[1] == wid and current[2] == fp:
        _inc_counter("interactive_noop")
        logger.debug(
            "Interactive probe noop: source=%s user=%d thread=%s window=%s fp=%s",
            source,
            user_id,
            thread_id,
            wid,
            fp[:8],
        )
        return True

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    await _do_clear_status_message(bot, user_id, tid)
    keyboard = _build_interactive_keyboard(wid, ui_name=content.name)
    thread_kwargs = _send_kwargs(thread_id)

    # Window switched in same thread: delete stale interactive message first.
    if current and current[1] != wid:
        await _clear_interactive_render(bot, user_id, tid)
        current = None

    if current:
        msg_id = current[0]
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            _interactive_render_state[ikey] = (msg_id, wid, fp)
            set_interactive_mode(user_id, wid, thread_id)
            set_interactive_msg_id(user_id, msg_id, thread_id)
            _inc_counter("interactive_edit")
            logger.debug(
                "Interactive probe edit: source=%s user=%d thread=%s window=%s fp=%s",
                source,
                user_id,
                thread_id,
                wid,
                fp[:8],
            )
            return True
        except RetryAfter:
            raise
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                _interactive_render_state[ikey] = (msg_id, wid, fp)
                set_interactive_mode(user_id, wid, thread_id)
                set_interactive_msg_id(user_id, msg_id, thread_id)
                _inc_counter("interactive_noop")
                logger.debug(
                    "Interactive probe noop-edit: source=%s user=%d thread=%s window=%s fp=%s",
                    source,
                    user_id,
                    thread_id,
                    wid,
                    fp[:8],
                )
                return True
            logger.debug(
                "Interactive probe edit failed: source=%s user=%d thread=%s window=%s error=%s",
                source,
                user_id,
                thread_id,
                wid,
                e,
            )
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        except Exception as e:
            logger.debug(
                "Interactive probe edit failed: source=%s user=%d thread=%s window=%s error=%s",
                source,
                user_id,
                thread_id,
                wid,
                e,
            )
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        link_preview_options=NO_LINK_PREVIEW,
        **thread_kwargs,  # type: ignore[arg-type]
    )
    if not sent:
        return False

    _interactive_render_state[ikey] = (sent.message_id, wid, fp)
    set_interactive_mode(user_id, wid, thread_id)
    set_interactive_msg_id(user_id, sent.message_id, thread_id)
    _inc_counter("interactive_send")
    logger.debug(
        "Interactive probe send: source=%s user=%d thread=%s window=%s fp=%s",
        source,
        user_id,
        thread_id,
        wid,
        fp[:8],
    )
    return True


# Backward-compatible alias for older tests/callers.
_process_interactive_probe_task = _process_pane_probe_task


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=_ensure_html(full_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                await _send_task_images(bot, chat_id, task)
                await _check_and_send_status(bot, user_id, wid, task.thread_id)
                return
            except RetryAfter:
                raise
            except BadRequest as e:
                # Edit target already has identical content; treat as success.
                if "message is not modified" in str(e).lower():
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = strip_sentinels(task.text or full_text)
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message
            except Exception:
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = strip_sentinels(task.text or full_text)
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )

        if sent:
            last_msg_id = sent.message_id

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 3.5 Probe interactive UI right after tool_use to preserve ordering.
    # Run inside the same worker task so it cannot race with poller callbacks.
    if task.content_type == "tool_use" and task.window_id:
        ui_visible = await _process_pane_probe_task(
            bot,
            user_id,
            MessageTask(
                task_type="pane_probe",
                window_id=task.window_id,
                thread_id=task.thread_id,
                source="tool_use",
                allow_status=False,
            ),
        )
        if ui_visible:
            # Send images and return - don't send status after UI
            await _send_task_images(bot, chat_id, task)
            return

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task)

    # 5. After content, check and send status
    await _check_and_send_status(bot, user_id, wid, task.thread_id)


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_ensure_html(content_text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain text with sentinels stripped
            plain = strip_sentinels(content_text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # Message might be deleted or too old, caller will send new message
            return None


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Claude is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_html(status_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e:
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(skey, None)
                    await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Claude is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    # Skip if there are more messages pending in the queue
    queue = get_message_queue(user_id, thread_id)
    if queue and not queue.empty():
        return
    await _process_pane_probe_task(
        bot,
        user_id,
        MessageTask(
            task_type="pane_probe",
            window_id=window_id,
            thread_id=thread_id,
            source="status_check",
            allow_status=True,
        ),
    )


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    wait_for_delivery: bool = False,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d thread=%s window_id=%s content_type=%s",
        user_id,
        thread_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id, thread_id)
    completion_fut: asyncio.Future[None] | None = None
    if wait_for_delivery:
        completion_fut = asyncio.get_running_loop().create_future()

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        source="monitor",
        completion_fut=completion_fut,
    )
    queue.put_nowait(task)
    if completion_fut:
        await completion_fut


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    key = _queue_key(user_id, thread_id)
    flood_end = _flood_until.get(key, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id, thread_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
            source="status",
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id, source="status")

    queue.put_nowait(task)


async def enqueue_pane_probe(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    source: str = "",
    allow_status: bool = True,
) -> None:
    """Enqueue pane probe with lightweight coalescing."""
    tid = thread_id or 0
    pkey = (user_id, tid, window_id)
    if pkey in _pane_probe_pending:
        _inc_counter("probe_coalesced")
        _inc_counter("duplicate_suppressed")
        logger.debug(
            "Pane probe coalesced: source=%s user=%d thread=%s window=%s",
            source or "",
            user_id,
            thread_id,
            window_id,
        )
        return

    _inc_counter("probe_enqueued")
    _pane_probe_pending.add(pkey)
    queue = get_or_create_queue(bot, user_id, thread_id)
    queue.put_nowait(
        MessageTask(
            task_type="pane_probe",
            window_id=window_id,
            thread_id=thread_id,
            source=source or "manual",
            allow_status=allow_status,
        )
    )


async def enqueue_interactive_clear(
    bot: Bot,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Enqueue interactive UI clear task."""
    queue = get_or_create_queue(bot, user_id, thread_id)
    queue.put_nowait(
        MessageTask(task_type="interactive_clear", thread_id=thread_id, source="manual")
    )


async def enqueue_interactive_probe(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    source: str = "",
) -> None:
    """Backward-compatible alias for enqueue_pane_probe."""
    await enqueue_pane_probe(
        bot=bot,
        user_id=user_id,
        window_id=window_id,
        thread_id=thread_id,
        source=source,
        allow_status=True,
    )


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


def clear_interactive_render_state(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive render tracking for a user/topic."""
    tid = thread_id or 0
    _interactive_render_state.pop((user_id, tid), None)
    clear_interactive_tracking(user_id, thread_id)
    # Drop pending probes for this user/thread to avoid stale re-renders.
    keys_to_remove = [
        key
        for key in _pane_probe_pending
        if key[0] == user_id and (thread_id is None or key[1] == tid)
    ]
    for key in keys_to_remove:
        _pane_probe_pending.discard(key)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    _interactive_render_state.clear()
    _pane_probe_pending.clear()
    logger.info("Message queue workers stopped")
