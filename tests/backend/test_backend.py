from aiogram import Router

from app import hello
from app.access import AllowlistMiddleware


def test_hello_router_mounted(bot):
    routers = list(bot.routers())
    assert hello.router in routers
    assert all(isinstance(r, Router) for r in routers)


def test_allowlist_middleware_registered(bot):
    middlewares = list(bot.outer_middlewares())
    assert any(isinstance(m, AllowlistMiddleware) for m in middlewares)
