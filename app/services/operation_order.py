"""Single-process ordering for retained data and a full-record clear.

Ordinary operations share the gate, so an idle object's management does not
wait for an unrelated model turn. A clear fences new work and drains actual
workers before changing the record generation.
"""

import asyncio
from contextlib import asynccontextmanager


async def completed_awaitable(awaitable):
    """Finish an ordered task even if its caller is repeatedly cancelled."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def completed_worker(function, *args, **kwargs):
    """Keep caller locks until native work ends, even on disconnection."""
    return await completed_awaitable(asyncio.to_thread(function, *args, **kwargs))


class OperationOrder:
    def __init__(self):
        self._condition = asyncio.Condition()
        self._active = 0
        self._clearing = False
        self._clear_lock = asyncio.Lock()
        self.management_lock = asyncio.Lock()
        self._quotes_condition = asyncio.Condition()
        self._quote_readers: dict[str, int] = {}
        self._quote_waiting: set[str] = set()
        self._quote_mutating: set[str] = set()

    async def begin_quote_read(self, quote_id, *, continuing=False):
        async with self._quotes_condition:
            # A turn already holding a quote lease can finish its remaining
            # reads while management is waiting for it. This prevents a bulk
            # purge from deadlocking a turn that queries several drafts.
            await self._quotes_condition.wait_for(lambda:
                quote_id not in self._quote_mutating and
                (continuing or quote_id not in self._quote_waiting))
            self._quote_readers[quote_id] = self._quote_readers.get(quote_id, 0) + 1

    async def end_quote_reads(self, quote_ids):
        async with self._quotes_condition:
            for quote_id in quote_ids:
                count = self._quote_readers[quote_id] - 1
                if count:
                    self._quote_readers[quote_id] = count
                else:
                    del self._quote_readers[quote_id]
            self._quotes_condition.notify_all()

    @asynccontextmanager
    async def manage_quotes(self, quote_ids):
        # Management callers already hold the single management lock.
        targets = set(quote_ids)
        try:
            async with self._quotes_condition:
                self._quote_waiting.update(targets)
                await self._quotes_condition.wait_for(lambda:
                    not any(self._quote_readers.get(key, 0) for key in targets))
                self._quote_waiting.difference_update(targets)
                self._quote_mutating.update(targets)
            yield
        finally:
            async with self._quotes_condition:
                self._quote_waiting.difference_update(targets)
                self._quote_mutating.difference_update(targets)
                self._quotes_condition.notify_all()

    @property
    def clearing(self):
        return self._clearing

    @asynccontextmanager
    async def ordinary(self):
        async with self._condition:
            # Reject, rather than queue a new write behind a clear whose
            # generation it could not have observed.
            if self._clearing:
                raise RuntimeError("record_clear_in_progress")
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def clear(self):
        async with self._clear_lock:
            try:
                async with self._condition:
                    self._clearing = True
                    await self._condition.wait_for(lambda: self._active == 0)
                yield
            finally:
                async with self._condition:
                    self._clearing = False
                    self._condition.notify_all()
