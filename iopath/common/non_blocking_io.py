# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.

import concurrent.futures
import io
import logging
from dataclasses import dataclass
from queue import Queue
from threading import Thread
from typing import Callable, IO, Optional, Union


@dataclass
class PathData:
    """
    Manage the IO job queue and polling thread for a single path.
    """
    queue: Queue
    thread: Thread


class NonBlockingIOManager:
    """
    All `opena` calls pass through this class so that it can
    keep track of the threads for proper cleanup at the end
    of the script. Each path that is opened with `opena` is
    assigned a single queue and polling thread that is kept
    open until it is cleaned up by `PathManager.join()`.
    """
    # Ensure `NonBlockingIOManager` is a singleton.
    __instance = None
    def __new__(cls, *args, **kwargs):
        if cls.__instance is None:
            cls.__instance = object.__new__(cls, *args, **kwargs)
            cls.__instance._path_to_data = {}
            # Keep track of a thread pool that `NonBlockingIO` instances
            # add jobs to.
            cls.__instance._pool = concurrent.futures.ThreadPoolExecutor()
        return cls.__instance

    def get_non_blocking_io(
        self,
        path: str,
        mode: str = "r",
        buffering: int = -1,
        encoding: Optional[str] = None,
        errors: Optional[str] = None,
        newline: Optional[str] = None,
        closefd: bool = True,
        opener: Optional[Callable] = None,
    ) -> Union[IO[str], IO[bytes]]:
        """
        Called by `PathHandler._opena` with the path and returns
        a `NonBlockingIO` instance.
        """
        if path not in self._path_to_data:
            queue = Queue()
            t = Thread(target=self._poll_jobs, args=(queue,))
            t.start()
            self._path_to_data[path] = PathData(queue, t)

        return NonBlockingIO(
            path,
            mode,
            notify_manager=lambda io_callable: (
                self._path_to_data[path].queue.put(io_callable)
            ),
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            closefd=closefd,
            opener=opener,
        )

    def _poll_jobs(self, queue: Optional[Callable[[], None]]) -> None:
        """
        A single thread runs this loop. It waits for an IO callable to be
        placed in a specific path's `Queue`. It then waits for the IO job
        to be completed before looping to ensure write order.
        """
        while True:
            # This item can be any of:
            #   - file.write(b)
            #   - file.close()
            #   - None
            item = queue.get()                      # Blocks until item read.
            if item is None:                        # Thread join signal.
                break
            self._pool.submit(item).result()        # Wait for job to finish.

    def _join(self, path: Optional[str] = None) -> bool:
        """
        Cleans up the ThreadPool and joins all threads.

        Args:
            path (str): Pass in a file path and will wait for the
                asynchronous jobs to be completed for that file path.
                If no path is passed in, then all threads operating
                on all file paths will be joined.
        """
        if path and path not in self._path_to_data:
            raise ValueError(
                f"{path} has no async IO associated with it. "
                f"Make sure `opena({path})` is called first."
            )
        # If a `_close` call fails, we print the error and continue
        # closing the rest of the IO objects.
        paths_to_close = [path] if path else list(self._path_to_data.keys())
        success = True
        for _path in paths_to_close:
            try:
                path_data = self._path_to_data.pop(_path)
                path_data.queue.put(None)
                path_data.thread.join()
            except Exception:
                logger = logging.getLogger(__name__)
                logger.exception(
                    f"`NonBlockingIO` thread for {_path} failed to join."
                )
                success = False
        if not path:
            self._pool.shutdown()
        return success


# NOTE: We currently only support asynchronous writes (not reads).
class NonBlockingIO(io.IOBase):
    def __init__(
        self,
        path: str,
        mode: str,
        notify_manager: Callable[[Callable[[], None]], None],
        buffering: int = -1,
        encoding: Optional[str] = None,
        errors: Optional[str] = None,
        newline: Optional[str] = None,
        closefd: bool = True,
        opener: Optional[Callable] = None,
    ) -> None:
        """
        Returned to the user on an `opena` call. Uses a Queue to manage the
        IO jobs that need to be run to ensure order preservation and a
        polling Thread that checks the Queue. Implementation for these are
        lifted to `NonBlockingIOManager` since `NonBlockingIO` closes upon
        leaving the context block.

        NOTE: Writes to the same path are serialized so they are written in
        the same order as they were called but writes to distinct paths can
        happen concurrently.

        Args:
            path (str): a URI that implements the `PathHandler._opena` method.
            mode (str): currently must be "w" or "wb" as we only implement an
                async writing feature.
            notify_manager (Callable): a callback function passed in from the
                `NonBlockingIOManager` so that all IO jobs can be stored in
                the manager.
        """
        super().__init__()
        self._path = path
        self._mode = mode
        self._notify_manager = notify_manager

        # `_file` will be closed by context manager exit or when `.close()`
        # is called explicitly.
        self._file = open(  # noqa: P201
            self._path,
            self._mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            closefd=closefd,
            opener=opener,
        )

    @property
    def name(self) -> str:
        return self._path

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        """
        Called on `f.close()` or automatically by the context manager.
        We add the `close` call to the file's queue to make sure that
        the file is not closed before all of the jobs are complete.
        """
        self._notify_manager(lambda: self._file.close())

    def write(self, b: Union[bytes, bytearray]) -> None:
        """
        Called on `f.write()`. Gives the manager the write job to call.
        """
        self._notify_manager(lambda: self._file.write(b))
