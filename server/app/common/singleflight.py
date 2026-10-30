import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Singleflight(Generic[T]):
    """
    Több azonos kulcsú párhuzamos kérés egyetlen közös háttértaskot vár meg.
    Megszakításbiztos: ha egy vagy több várakozó lecsatlakozik (kliens megszakítás),
    a háttértask az asyncio.shield védelem miatt nem szakad meg, így a többi várakozó
    sikeresen megkapja az eredményt. A regiszter takarítása a task saját befejezéséhez
    kötött, nem a várakozók finally ágához.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[T]] = {}

    async def run(
        self,
        key: str,
        coro_fn: Callable[[], Coroutine[Any, Any, T]],
    ) -> T:
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(coro_fn())
            self._tasks[key] = task

            def _on_done(t: asyncio.Task[T]) -> None:
                if not t.cancelled():
                    t.exception()
                if self._tasks.get(key) is t:
                    self._tasks.pop(key, None)

            task.add_done_callback(_on_done)

        return await asyncio.shield(task)

    def is_running(self, key: str) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def cancel(self, key: str) -> bool:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def cancel_all(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

