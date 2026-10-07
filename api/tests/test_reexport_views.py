"""`api/`'s old module paths are live views of `klado_shared/`, not re-export lists.

Six modules moved into the shared package during the admin-console work, and the files
they left behind (`api/services/auth_store.py`, `api/core/modules.py`, …) are shims. The
mechanism is `klado_shared/_reexport.py`; this file pins the behaviour that makes the
shims safe, because the failure mode of getting it wrong is not a crash.

⚠️ **A broken view does not fail loudly. It fails by making tests pass.** A shim that
forwards reads but not writes still looks like a working re-export to every reader, and
`patch.object(auth_store, "_db")` still applies, still exits cleanly, and still leaves the
function under test talking to the real database. The fake is simply never consulted.
Moving `auth_store` into `klado_shared/` with an ordinary re-export list cost 37 assertions
in `test_account_lifecycle.py` and `test_auth_colleagues.py` with zero errors — every one
of them had a fake attached to an object nothing read.

So the tests below are about *reach*, not about values: does a stub written through the
old path arrive where the function looks the name up, and does it get put back afterwards.
Test 3 and 4 exist because that is where it actually broke.

Nothing here needs a server or a database.
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _path in (REPO_DIR, API_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from klado_shared import _reexport, accounts, lifecycle, modules  # noqa: E402
from core import modules as modules_shim                      # noqa: E402
from services import account_lifecycle as lifecycle_shim      # noqa: E402
from services import auth_store as auth_store_shim            # noqa: E402

#: (shim, implementation) for every pair that exists. Stated as a list so a future move
#: that forgets to add its pair fails here rather than being discovered when somebody
#: writes a patch against the shim and wonders why it does nothing.
VIEWS = [
    ("services.auth_store", auth_store_shim, accounts),
    ("services.account_lifecycle", lifecycle_shim, lifecycle),
    ("core.modules", modules_shim, modules),
]


class ReadForwardingTests(unittest.TestCase):
    """Reads must resolve to the implementation's own objects, not copies."""

    def test_a_public_name_is_the_same_object(self):
        self.assertIs(auth_store_shim.get_user_by_id, accounts.get_user_by_id)
        self.assertIs(lifecycle_shim.GRACE_DAYS, lifecycle.GRACE_DAYS)
        self.assertIs(modules_shim.DATACENTER, modules.DATACENTER)

    def test_an_underscore_name_is_forwarded_too(self):
        """`_public` and `_db` are called from `routers/`. A star-import shim would drop
        every one of them, and the failure would be an AttributeError on a rare path."""
        self.assertIs(auth_store_shim._public, accounts._public)
        self.assertIs(auth_store_shim._ensure_schema, accounts._ensure_schema)

    def test_a_name_neither_module_has_says_where_to_look(self):
        """The message is the payload. The caller's next question is always "so where
        does it live?", and an AttributeError that does not answer it costs a search."""
        with self.assertRaises(AttributeError) as caught:
            auth_store_shim.issue_invite_code
        message = str(caught.exception)
        self.assertIn("issue_invite_code", message)
        self.assertIn("klado_shared.accounts", message)

    def test_dir_is_the_union(self):
        listed = dir(auth_store_shim)
        self.assertIn("get_user_by_id", listed)
        self.assertIn("__doc__", listed)


class WriteForwardingTests(unittest.TestCase):
    """The load-bearing half: a stub written through the shim has to arrive where the
    function under test resolves the name — in the *implementation's* globals."""

    def test_a_stub_on_the_shim_reaches_the_implementation(self):
        sentinel = object()
        with patch.object(auth_store_shim, "_db", return_value=sentinel):
            self.assertIs(accounts._db(), sentinel)

    def test_the_stub_is_visible_through_the_shim_while_it_is_active(self):
        sentinel = object()
        with patch.object(auth_store_shim, "_db", return_value=sentinel):
            self.assertIs(auth_store_shim._db(), sentinel)

    def test_a_second_patch_of_the_same_name_also_works(self):
        """⚠️ This is the test that was written after the break, not before it.

        `unittest.mock` records an attribute as "local" only if it is already in
        `target.__dict__`, and a forwarded name is not. So on exit it calls
        `delattr(shim, name)`. When `__delattr__` forwarded that to the implementation —
        the obvious thing to write — it *deleted the real function*, and the next test
        that patched the same name died with
        `AttributeError: module 'klado_shared.accounts' has no attribute
        'get_user_by_id'` from inside the code under test.

        Only the second patch fails, so it presents as a flaky test. Running two in a
        row is the whole assertion.
        """
        original = accounts.get_user_by_id
        with patch.object(auth_store_shim, "get_user_by_id", return_value={"id": 1}):
            pass
        with patch.object(auth_store_shim, "get_user_by_id", return_value={"id": 2}):
            self.assertEqual(accounts.get_user_by_id(9), {"id": 2})
        self.assertIs(accounts.get_user_by_id, original)

    def test_the_value_is_restored_afterwards(self):
        original = accounts._db
        with patch.object(auth_store_shim, "_db"):
            self.assertIsNot(accounts._db, original)
        self.assertIs(accounts._db, original)

    def test_nested_patches_unwind_in_order(self):
        """LIFO, because that is what a stack is for — and a single saved value would
        restore the *outer* stub as the final state, leaking a MagicMock into the next
        test in the file."""
        real = accounts.get_user_by_id
        outer, inner = object(), object()
        with patch.object(auth_store_shim, "get_user_by_id", outer):
            with patch.object(auth_store_shim, "get_user_by_id", inner):
                self.assertIs(accounts.get_user_by_id, inner)
            self.assertIs(accounts.get_user_by_id, outer)
        self.assertIs(accounts.get_user_by_id, real)

    def test_deleting_a_forwarded_name_refuses_instead_of_destroying_it(self):
        """An ordinary `del` must not be able to reach into shared code the other process
        is using. The shim does not own this name; removing it is not its to do."""
        original = accounts.get_user_by_id
        with self.assertRaises(AttributeError):
            del auth_store_shim.get_user_by_id
        self.assertIs(accounts.get_user_by_id, original)

    def test_a_name_the_implementation_does_not_own_stays_local(self):
        """A typo must create a harmless attribute, not inject a name into a module two
        processes share — and reading it back has to give back what was set."""
        auth_store_shim.not_a_real_name = "x"
        try:
            self.assertEqual(auth_store_shim.not_a_real_name, "x")
            self.assertFalse(hasattr(accounts, "not_a_real_name"))
        finally:
            del auth_store_shim.not_a_real_name
        self.assertFalse(hasattr(auth_store_shim, "not_a_real_name"))

    def test_the_module_can_still_be_retyped(self):
        """`__setattr__` intercepts every assignment, and `__class__` has to be let
        through — otherwise the module can never be re-typed, and tooling that swaps a
        module object (a stub loader, a test double for the module itself) breaks on it.

        Run on a throwaway module rather than a real shim: CPython only permits a
        `__class__` assignment to a *more derived* type, so putting it back is not
        guaranteed, and a test that leaves a real shim un-restorable would be a far worse
        bug than the one it is looking for.
        """
        probe = types.ModuleType("klado_reexport_probe")
        sys.modules[probe.__name__] = probe
        try:
            _reexport.live_view(accounts, probe.__name__)
            self.assertEqual(type(probe).__name__, "_LiveView")
            probe.__class__ = type("Narrower", (type(probe),), {})
            self.assertEqual(type(probe).__name__, "Narrower")
            # A real type change, not a dict entry that merely looks like one.
            self.assertIs(probe.get_user_by_id, accounts.get_user_by_id)
        finally:
            del sys.modules[probe.__name__]


class ViewInventoryTests(unittest.TestCase):
    """The list above is the list. A move that adds a shim and forgets this file is a
    move whose shim is untested."""

    def test_every_declared_pair_really_is_a_view(self):
        for name, shim, impl in VIEWS:
            with self.subTest(view=name):
                self.assertEqual(getattr(shim, "__reexports__", None),
                                 getattr(impl, "__name__", None))
                self.assertTrue(hasattr(shim, "__getattr__"), name)
                self.assertEqual(type(shim).__name__, "_LiveView", name)

    def test_the_shared_package_owns_the_implementations(self):
        """Stated so nobody 'fixes' a failing shim by copying a body back into `api/`."""
        for name, shim, impl in VIEWS:
            with self.subTest(view=name):
                self.assertIn(impl.__name__.split(".")[0], ("klado_shared",))
                self.assertNotEqual(shim.__name__, impl.__name__, name)

    def test_live_view_refuses_to_run_outside_its_own_module(self):
        with self.assertRaises(RuntimeError):
            _reexport.live_view(accounts, "not.a.module.that.exists")


if __name__ == "__main__":
    unittest.main()
