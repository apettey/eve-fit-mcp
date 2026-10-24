"""End-to-end: spawn the server over stdio and drive it like an MCP client would."""
import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data(result):
    assert not result.is_error, result.content
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def _run():
    params = StdioServerParameters(command=sys.executable, args=["-m", "eve_fit_mcp.server"], cwd=ROOT)
    with open(os.path.join(ROOT, "tests", "fixtures", "rifter.eft"), encoding="utf-8") as f:
        eft = f.read()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert {"load_fit", "optimize_fit", "try_candidates", "set_module"} <= tools

            fit = _data(await s.call_tool("load_fit", {"eft": eft}))
            fid = fit["fit_id"]
            assert fit["stats"]["ship"] == "Rifter"

            what_if = _data(await s.call_tool("try_candidates", {"fit_id": fid, "slot": "low:1"}))
            assert what_if["results"] and what_if["current"] == "Gyrostabilizer II"

            bad = await s.call_tool("set_module", {"fit_id": fid, "slot": "low:1", "item": "Stasis Webifier II"})
            assert bad.is_error

            opt = _data(await s.call_tool("optimize_fit", {
                "fit_id": fid, "objective": {"dps_total": 1, "ehp_total": 0.5},
                "pools": {"low": ["Gyrostabilizer II", "Damage Control II"]}}))
            assert opt["feasible"] and opt["fit_id"] != fid

            cmp_ = _data(await s.call_tool("compare_fits", {"fit_ids": [fid, opt["fit_id"]]}))
            assert len(cmp_["fits"]) == 2
            return opt


def test_stdio_round_trip():
    opt = anyio.run(_run)
    assert opt["stats"]["valid"]
