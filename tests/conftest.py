"""Lets integration tests be written as `async def test_...`.

Eight lines here beats adding pytest-asyncio as a dependency for the handful of
tests that drive command callbacks.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True
