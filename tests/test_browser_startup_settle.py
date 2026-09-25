"""Startup settling must never choose between competing browser windows."""
from copy import deepcopy
import unittest

from locua.engine.prototype.cli import Cancelled, prepare_owned_browser


def window(identity):
    return dict(pid=10, window_id=identity, layer=0, is_on_screen=True,
                bounds=dict(width=900, height=700), title="", app_name="Browser")


class Cancel:
    stopped = False
    def __init__(self, stop_on_wait=False):
        self.waits = []; self.stop_on_wait = stop_on_wait
    def is_set(self): return self.stopped
    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = self.stop_on_wait


class Owner:
    def __init__(self, samples): self.samples = samples; self.calls = []
    def start_session(self, session): pass
    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == "browser_prepare":
            return {}, dict(prepared=True, action="launched_isolated_browser", prepared_pid=10)
        if name == "list_windows":
            sample = self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]
            return {}, dict(windows=deepcopy(sample))
        if name == "get_browser_state":
            return {}, dict(binding_quality="exact", endpoint_access_class="driver_owned",
                            mutation_allowed=True, target_id="target",
                            tabs=[dict(tab_id="tab", url="about:blank")])
        if name == "browser_navigate": return {}, {}
        raise AssertionError(name)


class StartupSettleTests(unittest.TestCase):
    def test_transient_competitor_settles_before_exact_binding(self):
        owner = Owner([[window(20), window(21)], [window(20)]])
        state = {}; cancel = Cancel()
        prepare_owned_browser(owner, "session", "http://127.0.0.1/", cancel, state)
        self.assertEqual(cancel.waits, [.3])
        self.assertEqual(len(state["browser_startup_window_samples"]), 2)
        self.assertEqual([args["window_id"] for name, args in owner.calls if name == "get_browser_state"], [20])
        self.assertEqual(sum(name == "browser_navigate" for name, _ in owner.calls), 1)

    def test_persistent_ambiguity_stops_without_binding_or_navigation(self):
        owner = Owner([[window(20), window(21)]])
        with self.assertRaisesRegex(RuntimeError, "remain ambiguous"):
            prepare_owned_browser(owner, "session", "http://127.0.0.1/", Cancel(), {})
        self.assertEqual(sum(name == "list_windows" for name, _ in owner.calls), 20)
        self.assertFalse(any(name in ("get_browser_state", "browser_navigate") for name, _ in owner.calls))

    def test_cancel_during_settling_never_binds(self):
        owner = Owner([[window(20), window(21)]])
        with self.assertRaises(Cancelled):
            prepare_owned_browser(owner, "session", "http://127.0.0.1/", Cancel(True), {})
        self.assertEqual(sum(name == "list_windows" for name, _ in owner.calls), 1)


if __name__ == "__main__": unittest.main()
