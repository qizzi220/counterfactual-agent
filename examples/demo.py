import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from counterfactual_agent.core import CounterfactualLab


def checked(result):
    if not result.get("ok"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def run(directory: Path):
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=False)
    data_root = directory / "data"
    data_root.mkdir()
    database = data_root / "orders.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, amount INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)", [(1, "A", 10), (2, "B", 20)]
        )

    lab = CounterfactualLab(data_root=data_root, state_root=directory / "state")
    task = checked(
        lab.register_task(
            name="count-all-orders",
            question="Count all orders, including orders with no customer identifier.",
            database_path="orders.sqlite",
            reference_sql="SELECT COUNT(*) FROM orders",
            tags=["count", "nullable"],
        )
    )
    task_id = task["task_id"]
    checked(
        lab.add_variant(
            task_id,
            "anonymous-order",
            [{"kind": "insert", "table": "orders", "values": {"id": 3, "customer": None, "amount": 10}}],
            "practice",
        )
    )
    checked(
        lab.add_variant(
            task_id,
            "two-anonymous-orders",
            [
                {"kind": "insert", "table": "orders", "values": {"id": 3, "customer": None, "amount": 0}},
                {"kind": "insert", "table": "orders", "values": {"id": 4, "customer": None, "amount": 40}},
            ],
            "holdout",
        )
    )
    failed = checked(lab.evaluate(task_id, "SELECT COUNT(customer) FROM orders", "practice"))
    passed = checked(lab.evaluate(task_id, "SELECT COUNT(*) FROM orders", "practice"))
    holdout = checked(lab.evaluate(task_id, "SELECT COUNT(*) FROM orders", "holdout"))
    lesson = checked(
        lab.propose_lesson(
            task_id,
            failed["attempt_id"],
            passed["attempt_id"],
            "Counting rows with optional identifiers",
            "When every row must be counted and customer is nullable, COUNT(customer) omits null identifiers; use COUNT(*) and test a permitted null case.",
            ["count", "nullable"],
        )
    )
    activated = checked(lab.activate_lesson(lesson["lesson_id"]))
    memory = checked(lab.retrieve_lessons(["count"], 5))
    print(
        json.dumps(
            {
                "scope": "Synthetic functional demonstration; no model was trained or evaluated.",
                "failed_attempt": failed,
                "corrected_attempt": passed,
                "holdout_attempt": holdout,
                "activation": activated,
                "memory": memory,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Run the synthetic counterfactual memory demonstration.")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    run(args.directory)


if __name__ == "__main__":
    main()
