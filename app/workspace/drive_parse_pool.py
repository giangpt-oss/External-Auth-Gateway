"""Supervise bounded, isolated parser processes without blocking asyncio."""
from __future__ import annotations

import asyncio
import itertools
import multiprocessing as mp
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from multiprocessing.connection import wait
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.workspace.parsers import ParsedChunk

ERROR_MESSAGES = {
    "DECOMPRESSION_TOO_LARGE": "Drive file exceeds 64 MiB uncompressed.",
    "RESPONSE_TOO_LARGE": "Drive file exceeds 8 MiB.",
    "UNSUPPORTED_FILE": "No parser for this Drive file format.",
    "PARSE_ERROR": "Drive file could not be parsed.",
}


def _child(channel, parser):
    with channel:
        channel.send(None)
        from app.workspace.service import WorkspaceError

        if parser is None:
            from app.workspace.parsers import parse_file

            parser = parse_file
        while True:
            try:
                mime, data = channel.recv()
            except EOFError:
                return
            try:
                chunks = parser(mime, data)
            except WorkspaceError as exc:
                code = exc.code if exc.code in ERROR_MESSAGES else "PARSE_ERROR"
                channel.send((code, None))
            except Exception:
                channel.send(("PARSE_ERROR", None))
            else:
                channel.send((None, chunks))


def _start(context, parser):
    parent, child = context.Pipe()
    process = context.Process(target=_child, args=(child, parser), daemon=True)
    try:
        process.start()
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    return process, parent


def _receive(channel, sentinel):
    if sentinel in wait([channel, sentinel]):
        raise EOFError
    return channel.recv()


def _exchange(channel, sentinel, mime, content, starting):
    if starting:
        _receive(channel, sentinel)
    channel.send((mime, content))
    return _receive(channel, sentinel)


def _stop(process, grace):
    if process.is_alive():
        process.terminate()
    process.join(grace)
    if process.is_alive():
        process.kill()
        process.join()


@dataclass
class _Request:
    mime: str
    content: bytes = field(repr=False)
    deadline: float | None
    result: asyncio.Future


class ParsePool:
    def __init__(self, *, processes=2, queue_size=8, timeout=30.0, terminate_grace=5.0,
                 parser=None):
        if not 1 <= processes <= 2 or not 1 <= queue_size <= 8:
            raise ValueError("Drive parsers support up to two processes and eight waiting files.")
        if not 0 < timeout <= 30 or not 0 <= terminate_grace <= 5:
            raise ValueError("Invalid Drive parser time limits.")
        # Docker workers run on Linux, where forkserver creates isolated child
        # processes without forking the active asyncio worker thread. Windows
        # has no forkserver and must use spawn.
        self._context = mp.get_context("spawn" if os.name == "nt" else "forkserver")
        self._processes = processes
        self._queue = asyncio.PriorityQueue(maxsize=queue_size)
        self._timeout = timeout
        self._grace = terminate_grace
        self._parser = parser
        self._sequence = itertools.count()
        self._supervisors: list[asyncio.Task] = []
        self._closed = False

    async def parse(self, mime: str, content: bytes, *, priority=False,
                    deadline: float | None = None) -> list[ParsedChunk]:
        from app.workspace.service import MAX_MEDIA_BYTES, WorkspaceError

        if self._closed:
            raise WorkspaceError("PARSER_UNAVAILABLE", "Drive parser pool is closed.")
        if len(content) > MAX_MEDIA_BYTES:
            raise WorkspaceError("RESPONSE_TOO_LARGE", ERROR_MESSAGES["RESPONSE_TOO_LARGE"])
        if deadline is not None and time.monotonic() >= deadline:
            raise WorkspaceError("DEADLINE_EXCEEDED", "Drive request deadline expired.")
        result = asyncio.get_running_loop().create_future()
        request = _Request(mime, content, deadline, result)
        try:
            self._queue.put_nowait((not priority, next(self._sequence), request))
        except asyncio.QueueFull as exc:
            raise WorkspaceError("PARSE_QUEUE_FULL", "Drive parser queue is full.") from exc
        if not self._supervisors:
            self._supervisors = [asyncio.create_task(self._supervise())
                                 for _ in range(self._processes)]
        try:
            if deadline is None:
                return await result
            return await asyncio.wait_for(result, timeout=max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise WorkspaceError("DEADLINE_EXCEEDED", "Drive request deadline expired.") from exc

    async def _supervise(self):
        process = channel = io = None
        try:
            while True:
                _, _, request = await self._queue.get()
                if request.result.done():
                    continue
                end = time.monotonic() + self._timeout
                if request.deadline is not None:
                    end = min(end, request.deadline)
                try:
                    starting = process is None
                    if starting:
                        start = asyncio.create_task(
                            asyncio.to_thread(_start, self._context, self._parser),
                        )
                        try:
                            process, channel = await asyncio.shield(start)
                        except asyncio.CancelledError:
                            process, channel = await start
                            raise
                    io = asyncio.create_task(asyncio.to_thread(
                        _exchange, channel, process.sentinel, request.mime, request.content, starting,
                    ))
                    code, chunks = await asyncio.wait_for(asyncio.shield(io),
                                                          timeout=max(0, end - time.monotonic()))
                    io = None
                    if code:
                        self._fail(request, code, ERROR_MESSAGES[code])
                    elif not request.result.done():
                        request.result.set_result(chunks)
                except TimeoutError:
                    if end == request.deadline:
                        self._fail(request, "DEADLINE_EXCEEDED", "Drive request deadline expired.")
                    else:
                        self._fail(request, "PARSE_TIMEOUT", "Drive file parser timed out.")
                    cleanup = asyncio.create_task(self._dispose(process, channel, io))
                    process = channel = io = None
                    await self._await_cleanup(cleanup)
                except (EOFError, OSError, ValueError):
                    self._fail(request, "PARSER_UNAVAILABLE", "Drive file parser stopped unexpectedly.")
                    if process is not None:
                        cleanup = asyncio.create_task(self._dispose(process, channel, io))
                        process = channel = io = None
                        await self._await_cleanup(cleanup)
                except asyncio.CancelledError:
                    self._fail(request, "PARSER_UNAVAILABLE", "Drive parser pool is closed.")
                    raise
        finally:
            if process is not None:
                await self._dispose(process, channel, io)

    @staticmethod
    def _fail(request, code, message):
        from app.workspace.service import WorkspaceError

        if not request.result.done():
            request.result.set_exception(WorkspaceError(code, message))

    @staticmethod
    async def _await_cleanup(cleanup):
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _dispose(self, process, channel, io):
        await asyncio.to_thread(_stop, process, self._grace)
        if io is not None:
            with suppress(EOFError, OSError, ValueError):
                await io
        channel.close()
        process.close()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        while not self._queue.empty():
            _, _, request = self._queue.get_nowait()
            self._fail(request, "PARSER_UNAVAILABLE", "Drive parser pool is closed.")
        for worker in self._supervisors:
            worker.cancel()
        await asyncio.gather(*self._supervisors, return_exceptions=True)
