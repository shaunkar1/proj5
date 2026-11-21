"""In-process stand-in for ProcessPoolExecutor.

sqlglot's optimizer and executor tests fan work out to spawned worker
processes.  Spawning is expensive (and the worker count follows the host's
core count), so tests run through this pool instead.  Arguments and results
still take a pickle round trip, like they do across a real process boundary.
Work is done when a result is asked for, so a test that stops at its first
failure does not pay for the rest.
"""

import pickle


class LazyFuture:
    def __init__(self, pool, fn, args, kwargs):
        self._pool = pool
        self._call = (fn, args, kwargs)
        self._state = None  # None: not run, else ("ok", value) or ("err", exception)

    def _run(self):
        if self._state is None:
            fn, args, kwargs = self._call
            self._call = None
            try:
                if self._pool._init_error is not None:
                    raise RuntimeError("initializer failed") from self._pool._init_error
                fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                value = fn2(*args2, **kwargs2)
                self._state = ("ok", pickle.loads(pickle.dumps(value)))
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as error:
                self._state = ("err", error)
        return self._state

    def result(self, timeout=None):
        kind, value = self._run()
        if kind == "err":
            raise value
        return value

    def exception(self, timeout=None):
        kind, value = self._run()
        return value if kind == "err" else None

    def done(self):
        return True

    def cancelled(self):
        return False

    def running(self):
        return False

    def cancel(self):
        return False

    def add_done_callback(self, fn):
        self._run()
        fn(self)


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._init_error = None
        if initializer is not None:
            try:
                initializer(*initargs)
            except BaseException as error:  # a real pool would report a broken pool
                self._init_error = error

    def submit(self, fn, /, *args, **kwargs):
        return LazyFuture(self, fn, args, kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        return (f.result() for f in futures)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install():
    import concurrent.futures
    import concurrent.futures.process

    real_wait = concurrent.futures.wait
    real_as_completed = concurrent.futures.as_completed

    def wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        fs = list(fs)
        if any(isinstance(f, LazyFuture) for f in fs):
            Done = type(real_wait([]))
            return Done(set(fs), set())
        return real_wait(fs, timeout=timeout, return_when=return_when)

    def as_completed(fs, timeout=None):
        fs = list(fs)
        if any(isinstance(f, LazyFuture) for f in fs):
            return iter(fs)
        return real_as_completed(fs, timeout=timeout)

    concurrent.futures.ProcessPoolExecutor = InlinePool
    concurrent.futures.process.ProcessPoolExecutor = InlinePool
    concurrent.futures.wait = wait
    concurrent.futures.as_completed = as_completed
