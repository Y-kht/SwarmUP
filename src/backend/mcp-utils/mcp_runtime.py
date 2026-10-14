# The MCP runtime of SwarmUP: the MCP sessions are asynchronous (the official mcp package runs on anyio), and the agents of a swarm are
# threads. One event loop runs in a thread of its own for the whole program, and every session of every agent lives in it. A thread that
# needs a session (an agent that calls a tool, a server that asks the user) goes through call(), which waits for the result as if it were an
# ordinary function. The tools of SwarmUP are ordinary functions too: a server runs them in a worker thread (mcp_kit.py), so the event loop
# never waits for a file, a command or the user.
import asyncio
import contextvars
import threading

import anyio
import anyio.to_thread


# The worker threads that run the tools and wait for the user: many agents can wait for a card at the same time.
WORKER_THREADS = 400


class McpRuntime:
    def __init__(self):
        self.loop = None
        self.ready = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.loop is not None and self.loop.is_running():
                return self.loop
            self.ready.clear()
            threading.Thread(target=self.serve, name="swarmup-mcp", daemon=True).start()
            self.ready.wait()
            return self.loop

    def serve(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.run_until_complete(self.prepare())
        self.ready.set()
        loop.run_forever()

    async def prepare(self):
        anyio.to_thread.current_default_thread_limiter().total_tokens = WORKER_THREADS

    def inside(self):
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    # Runs a coroutine in the loop and waits for it. A timeout cancels it in the loop as well, and gives TimeoutError.
    def call(self, coroutine, timeout=None):
        if self.inside():
            raise RuntimeError("A coroutine of the MCP loop cannot wait for the loop itself.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.start())
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise
        except BaseException:
            future.cancel()
            raise

    # Starts a coroutine in the loop without waiting for it (a session that stays open). It gives the future.
    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.start())


RUNTIME = McpRuntime()


def run(coroutine, timeout=None):
    return RUNTIME.call(coroutine, timeout)


def submit(coroutine):
    return RUNTIME.submit(coroutine)


# A value that follows one call of a tool through the event loop (the id of its request, for its cancellation).
CURRENT_CALL = contextvars.ContextVar("swarmupCall", default=None)
