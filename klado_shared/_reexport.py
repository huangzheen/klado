"""Turn a module into a *live view* of another module.

Why this exists
---------------
When shared code moves out of `api/` into `klado_shared/`, the old path has to keep
working — the main app has ~40 modules saying `from services import auth_store`, and
rewriting all of them would be a large diff for no behavioural gain. The old path becomes
a shim, and the naive shim is a trap:

```python
# naive: a re-export list
from klado_shared.accounts import create_user, get_user_by_id, _public, ...
```

Two things go wrong with that, and both are silent.

**Identity is lost.** `shim.create_user is accounts.create_user` is false for anything
you forgot to list. "There is one implementation" stops being a fact you can assert and
becomes a hope.

**Patching stops working, while still looking like it does.** This is the one that bit.
`patch.object(auth_store, "_db")` sets an attribute on the shim, while
`auth_store.search_users` resolves `_db` from the globals of the module that *defined* it
— `klado_shared/accounts.py`. The patch applies, the context manager exits cleanly, the
test is green, and the fake database was never consulted. Moving the accounts module cost
`test_account_lifecycle.py` and `test_auth_colleagues.py` 37 assertions with no error
anywhere: the fakes were attached to an object nothing read. A patch that quietly does
nothing is worse than a patch that fails, because it converts a broken test into a
passing one.

So a shim here is not a list. It is a **live view**: reads forward, and so do writes.

The implementation
------------------
⚠️ **A `__setattr__` written into a module's namespace does nothing.** Module attribute
assignment goes through `module`'s `tp_setattro`, which resolves `__setattr__` on the
module's *type*, never in its own `__dict__` — so the first version of this file defined
three hooks in `globals()` and forwarded reads perfectly while every write silently went
to the shim. (Reads are different: `__getattr__` *is* honoured from a module's own
namespace, which is why the read half looked right and hid the bug in the write half.)

The fix is to give the module a class:

    module.__class__ = _LiveView

Reassigning a module's `__class__` to a `ModuleType` subclass is supported for heap
types and is how libraries like Werkzeug make module proxies. After it, `getattr`,
`setattr` and `delattr` on the module object all reach the hooks below.

The rules
---------
* A name `impl` owns forwards on read, write and delete. Identity is therefore exact, and
  `unittest.mock` unwinds correctly because its own "was this attribute local?" check
  agrees with what actually happened.
* A name `impl` does NOT own stays in the shim's own namespace. So
  `auth_store.brand_new_thing = 1` is a local, harmless fact rather than a silent
  injection into a module the other process also uses — and reading it back gives back
  what you set, which is the only sensible behaviour for a typo.
* `__dir__` is the union, so completion does not hide half the module.

One implementation of this, on purpose. Six copies of the delegation logic is six places
for it to diverge, and the divergence would look exactly like a shim that quietly stopped
forwarding — which is the failure this file exists to make impossible.
"""
from __future__ import annotations

import sys
from types import ModuleType

#: Key under which the shim remembers what it overwrote. Underscored and unguessable on
#: purpose: it is a shim, not a module anybody should be reaching into.
_OVERRIDES = "_reexport_overrides"


def live_view(impl: ModuleType, shim_name: str) -> ModuleType:
    """Make the module named `shim_name` resolve to `impl` for every name `impl` owns.

    Call this as the last statement of the shim's body. Returns the module, so a caller
    that wants the object can have it without a second `sys.modules` lookup.

    `shim_name` is used only in the error message, and it names BOTH places: the caller's
    next question after an `AttributeError` is always "so where does it live?", and a
    message that answers it turns a confusing failure into a five-second one.
    """
    module = sys.modules.get(shim_name)
    if module is None:
        raise RuntimeError(
            f"live_view() called from {shim_name!r}, which is not in sys.modules — it "
            f"must be called at the end of that module's own body")
    impl_name = getattr(impl, "__name__", repr(impl))

    def __getattr__(self, name: str):
        try:
            return getattr(impl, name)
        except AttributeError:
            raise AttributeError(
                f"module {shim_name!r} has no attribute {name!r} — and {impl_name} does "
                f"not define it either"
            ) from None

    def __setattr__(self, name: str, value) -> None:
        # `__class__` has to bypass this hook or the module could not be re-typed later.
        if name == "__class__":
            object.__setattr__(self, name, value)
        elif hasattr(impl, name):
            # Write through to the implementation, because that is where the function
            # under test resolves the name from: `impl.search_users` looks `_db` up in
            # `impl.__dict__`, so a shadow stored only on the shim would stub nothing.
            # Record the previous value first — see `__delattr__` for why.
            self.__dict__.setdefault(_OVERRIDES, {}).setdefault(name, []).append(
                getattr(impl, name))
            setattr(impl, name, value)
        else:
            object.__setattr__(self, name, value)

    def __delattr__(self, name: str):
        # ⚠️ The load-bearing half, and the second version of this file got it wrong in
        # the most dangerous possible way.
        #
        # `unittest.mock` records an attribute as "local" only when it is already in
        # `target.__dict__`, and for a forwarded name it is not — so on exit mock calls
        # `delattr(shim, name)`. The obvious implementation forwards that to the
        # implementation, which **deletes the real function**: the first patch in a test
        # run works, and the second one of the same name raises
        # `AttributeError: module 'klado_shared.accounts' has no attribute
        # 'get_user_by_id'` from inside the code under test. Every test that patches a
        # name twice, in one run, fails — and only the second one, so it reads like a
        # flaky test rather than a broken view.
        #
        # So `del` means "undo my last write", not "remove the name". The stack (not a
        # single saved value) is what makes nested patches unwind in the right order.
        stack = self.__dict__.get(_OVERRIDES, {}).get(name)
        if stack:
            setattr(impl, name, stack.pop())
            return
        if name in self.__dict__:
            object.__delattr__(self, name)
            return
        # Never patched here, and not ours to remove. Deleting through to the
        # implementation would let an ordinary `del` destroy shared code that the other
        # process also uses; refusing is both safer and what a real module does for a name
        # it does not have.
        raise AttributeError(
            f"module {shim_name!r} has no attribute {name!r} to delete — it is owned by "
            f"{impl_name}, not by this view")

    def __dir__(self):
        return sorted(set(self.__dict__) | set(dir(impl)))

    # Built with `type()` rather than a `class` statement: a class body does not close
    # over the enclosing function's locals, so `class V(ModuleType): __getattr__ =
    # __getattr__` is a NameError — the inner name resolves in the class namespace, finds
    # nothing, and never looks outward. The hooks themselves are correct; the scoping is
    # not.
    live_view_type = type("_LiveView", (ModuleType,), {
        "__getattr__": __getattr__,
        "__setattr__": __setattr__,
        "__delattr__": __delattr__,
        "__dir__": __dir__,
        "__doc__": f"Live view of {impl_name} (see klado_shared/_reexport.py).",
    })

    module.__class__ = live_view_type
    # A marker, so a test can assert "this module is a view of THAT module" instead of
    # inferring it from behaviour. Read by api/tests/test_reexport_views.py.
    module.__reexports__ = impl_name
    module.__doc__ = (module.__doc__ or "") + f"\n\n(live view of {impl_name})\n"
    return module
