from datetime import date

import pytest

from hub.budget import BudgetExceeded, check_budget, record_usage


async def test_budget_blocks_after_limit(db, settings):
    await record_usage(db, 1, date(2026, 11, 2), 2_000_000, 0)
    with pytest.raises(BudgetExceeded):
        await check_budget(db, 1, date(2026, 11, 2), settings)
    await check_budget(db, 1, date(2026, 11, 3), settings)  # новый день — снова можно


async def test_budget_blocks_on_tokens_out(db, settings):
    await record_usage(db, 1, date(2026, 11, 2), 0, 200_000)
    with pytest.raises(BudgetExceeded):
        await check_budget(db, 1, date(2026, 11, 2), settings)


async def test_budget_is_per_user_and_accumulates(db, settings):
    d = date(2026, 11, 2)
    await record_usage(db, 1, d, 1_000_000, 10)
    await record_usage(db, 1, d, 1_000_000, 10)
    row = await (
        await db.execute("SELECT tokens_in, tokens_out FROM llm_usage WHERE user_id = 1")
    ).fetchone()
    assert (row["tokens_in"], row["tokens_out"]) == (2_000_000, 20)
    await check_budget(db, 2, d, settings)
    with pytest.raises(BudgetExceeded):
        await check_budget(db, 1, d, settings)
