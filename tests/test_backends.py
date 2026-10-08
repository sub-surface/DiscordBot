from __future__ import annotations

import unittest
from types import SimpleNamespace

from psychograph.backends import ModalBackend
from psychograph.settings import Settings
from psychograph.store import Store
from tests.helpers import make_bot


class FakeRemote:
    """Stands in for `worker.complete.remote`, optionally rejecting the `options` argument like an old deploy."""

    def __init__(self, result: dict, legacy: bool = False) -> None:
        self.result, self.legacy, self.calls = result, legacy, []

    async def aio(self, *args):
        self.calls.append(args)
        if self.legacy and len(args) > 5:
            raise TypeError("complete() takes 6 positional arguments but 7 were given")
        return self.result


def modal_backend(remote: FakeRemote) -> ModalBackend:
    backend = ModalBackend(Settings())
    backend._worker = SimpleNamespace(complete=SimpleNamespace(remote=remote))
    return backend


WORKER_REPLY = {
    "text": "hi", "completion_tokens": 3, "tokens_per_second": 44.0, "eval_seconds": 0.1, "cold": True,
    "boot": {"container_seconds": 21.5}, "worker": {"gpu": "T4", "context": 32768, "scaledown": 60, "thinking": False},
}


class ModalBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reports_cold_boot_and_scaledown(self) -> None:
        remote = FakeRemote(WORKER_REPLY)
        backend = modal_backend(remote)
        backend.thinking = True
        completion = await backend.complete([{"role": "user", "content": "hi"}])
        self.assertTrue(completion.cold)
        self.assertEqual(completion.boot_seconds, 21.5)
        self.assertEqual(backend.scaledown_seconds, 60)
        self.assertEqual(remote.calls[0][5], {"thinking": True, "top_k": Settings().top_k})

    async def test_old_worker_without_options_still_answers(self) -> None:
        remote = FakeRemote({"text": "hi"}, legacy=True)
        backend = modal_backend(remote)
        self.assertEqual((await backend.complete([])).text, "hi")
        self.assertEqual((await backend.complete([])).text, "hi")
        self.assertEqual([len(call) for call in remote.calls], [6, 5, 5])  # tried once, then remembered


class ThinkingStateTests(unittest.TestCase):
    def test_thinking_is_off_by_default_and_remembered(self) -> None:
        self.assertFalse(make_bot().backend.thinking)
        store = Store(":memory:")
        store.set_state("thinking", "on")
        from psychograph.bot import PsychographBot
        from tests.helpers import FakeBackend
        self.assertTrue(PsychographBot(Settings(), store=store, backend=FakeBackend()).backend.thinking)


if __name__ == "__main__":
    unittest.main()
