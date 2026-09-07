"""The tree must reach the model through `user_message`, and must NOT be in the system prompt.

The system block is the first cached block, so live state there costs the tools schema, the system
prompt and the whole transcript behind them on every edit. Nothing else in the suite observes either
method, so a slip in either direction — the tree left in `system_prompt`, or `user_message` handing
back the static header instead of the tree — runs green while the researcher loses its record or the
cache.
"""

from app.subagents.hypothesis_tree import HypothesisStatus, build_hypothesis_tree_tools


async def test_the_tree_rides_the_user_message_and_the_system_prompt_holds_only_method() -> None:
    add, update = build_hypothesis_tree_tools()
    await add.execute(node_id="H1", text="rents fell in 2026", parent=None)
    await update.execute(node_id="H1", status=HypothesisStatus.VERIFIED, finding="down 4% YoY")

    injected = (await update.user_message())["content"][0]["text"]
    system = await update.system_prompt()

    assert "rents fell in 2026" in injected and "down 4% YoY" in injected
    assert "HYPOTHESIS TREE" in injected, "an unlabelled list mid-turn says nothing about what it is"
    assert "rents fell in 2026" not in system, "live state in the cached block is the whole bug"


async def test_only_one_of_the_pair_injects_the_tree() -> None:
    add, update = build_hypothesis_tree_tools()
    await add.execute(node_id="H1", text="rents fell in 2026", parent=None)

    assert await add.user_message() is None, "two injections would send the tree twice every turn"
    assert await update.user_message() is not None
