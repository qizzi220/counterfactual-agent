import json
import sqlite3
import sys
from contextlib import closing

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result):
    assert not result.isError
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def test_stdio_experience_cycle(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    source = data_root / "orders.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)", [(1, "A"), (2, "B")])

    async def scenario():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "counterfactual_agent.server",
                "--data-root", str(data_root),
                "--state-root", str(tmp_path / "state"),
            ],
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                available = await session.list_tools()
                assert {tool.name for tool in available.tools} == {
                    "register_task", "add_variant", "evaluate", "propose_lesson",
                    "activate_lesson", "retrieve_lessons", "retire_lesson", "get_task",
                }

                async def call(tool_name, **arguments):
                    result = payload(await session.call_tool(tool_name, arguments))
                    assert result["ok"], result
                    return result

                task = await call(
                    "register_task",
                    name="all-orders",
                    question="Count all order rows, including orders without a customer.",
                    database_path="orders.sqlite",
                    reference_sql="SELECT COUNT(*) FROM orders",
                    tags=["count"],
                )
                task_id = task["task_id"]
                for partition, row_id in [("practice", 3), ("holdout", 4)]:
                    await call(
                        "add_variant", task_id=task_id, name=partition,
                        operations=[{"kind": "insert", "table": "orders", "values": {"id": row_id, "customer": None}}],
                        partition=partition,
                    )

                failed = await call("evaluate", task_id=task_id, candidate_sql="SELECT COUNT(customer) FROM orders")
                assert failed["status"] == "fail"
                corrected = await call("evaluate", task_id=task_id, candidate_sql="SELECT COUNT(*) FROM orders")
                assert corrected["status"] == "pass"
                held_out = await call(
                    "evaluate", task_id=task_id, candidate_sql="SELECT COUNT(*) FROM orders", partition="holdout"
                )
                assert held_out["status"] == "pass"
                lesson = await call(
                    "propose_lesson", task_id=task_id,
                    failing_attempt_id=failed["attempt_id"], passing_attempt_id=corrected["attempt_id"],
                    title="Count every row", rule="For a total row count, COUNT(*) includes rows with nullable identifiers.",
                    tags=["count"],
                )
                active = await call("activate_lesson", lesson_id=lesson["lesson_id"])
                assert active["status"] == "active"
                lessons = await call("retrieve_lessons", tags=["count"])
                assert [entry["lesson_id"] for entry in lessons["lessons"]] == [lesson["lesson_id"]]
                await call("get_task", task_id=task_id)
                await call("retire_lesson", lesson_id=lesson["lesson_id"])
                assert (await call("retrieve_lessons", tags=["count"]))["lessons"] == []

    anyio.run(scenario)
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
