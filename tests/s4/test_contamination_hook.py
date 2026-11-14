"""The eval's contamination filter reaches every setup through so.telem_client.RESULT_FILTER."""
import asyncio

from so import telem_client
from so.contracts import EngineRun, Result


def run(*urls):
    return {"tavily": EngineRun(engine="tavily", status="ok", results=[
        Result(rank=i, title=u, url=u, snippet="", engine="tavily") for i, u in enumerate(urls, 1)])}


def test_filter_is_off_by_default():
    runs = run("https://a.example", "https://huggingface.co/datasets/x")
    assert telem_client._filtered(runs) == runs


def test_filter_drops_and_renumbers_within_the_task():
    def drop_hf(results):
        return [r for r in results if "huggingface" not in r.url]

    async def inside():
        token = telem_client.RESULT_FILTER.set(drop_hf)
        try:
            out = telem_client._filtered(run("https://huggingface.co/datasets/x", "https://a.example"))
        finally:
            telem_client.RESULT_FILTER.reset(token)
        return out

    out = asyncio.run(inside())["tavily"].results
    assert [r.url for r in out] == ["https://a.example"] and out[0].rank == 1
    assert telem_client.RESULT_FILTER.get() is None  # reset, no leak to other tasks


def test_parallel_tasks_keep_their_own_filters():
    async def task(marker):
        telem_client.RESULT_FILTER.set(lambda rs: [r for r in rs if marker in r.url])
        await asyncio.sleep(0)
        return [r.url for r in telem_client._filtered(run("https://a.example", "https://b.example"))["tavily"].results]

    async def both():
        return await asyncio.gather(asyncio.create_task(task("a.")), asyncio.create_task(task("b.")))

    assert asyncio.run(both()) == [["https://a.example"], ["https://b.example"]]
