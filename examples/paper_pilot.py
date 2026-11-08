"""Deterministic SQLite pilot: one base, one development and one holdout mutation.

This deliberately designed demonstration does not train or evaluate an AI model.
The Python oracles implement the task descriptions independently of candidate SQL.
"""
import argparse
import collections
import json
import math
import sqlite3
from pathlib import Path


BASE = [(1, 'C01', 'paid', 10, '2026-10-10'),
        (2, 'C02', 'paid', 20, '2026-10-11')]

TASKS = [
    {
        'id': 'all_orders',
        'question': 'Number of all order rows, including rows with a null customer.',
        'guard': (3, None, 'paid', 30, '2026-10-12'),
        'holdout': (4, 'C04', 'paid', 0, '2026-10-13'),
        'oracle': lambda rows: len(rows),
        'valid': [
            'SELECT COUNT(*) FROM orders',
            'SELECT COALESCE(SUM(1), 0) FROM orders',
        ],
        'invalid': [
            'SELECT COUNT(customer) FROM orders',
            'SELECT COUNT(*) FROM orders WHERE amount > 0',
        ],
    },
    {
        'id': 'paid_revenue',
        'question': 'Sum of amounts for paid orders, including zero and negative amounts.',
        'guard': (3, 'C03', 'cancelled', 30, '2026-10-12'),
        'holdout': (4, 'C04', 'paid', -5, '2026-10-13'),
        'oracle': lambda rows: sum(r[3] for r in rows if r[2] == 'paid' and r[3] is not None),
        'valid': [
            "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid'",
            "SELECT COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) FROM orders",
        ],
        'invalid': [
            'SELECT COALESCE(SUM(amount), 0) FROM orders',
            "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND amount > 0",
        ],
    },
    {
        'id': 'distinct_customers',
        'question': 'Number of distinct non-null customer identifiers; case-sensitive.',
        'guard': (3, 'C01', 'paid', 30, '2026-10-12'),
        'holdout': (4, 'c01', 'paid', 30, '2026-10-13'),
        'oracle': lambda rows: len({r[1] for r in rows if r[1] is not None}),
        'valid': [
            'SELECT COUNT(DISTINCT customer) FROM orders',
            'SELECT COUNT(*) FROM (SELECT DISTINCT customer FROM orders WHERE customer IS NOT NULL)',
        ],
        'invalid': [
            'SELECT COUNT(customer) FROM orders',
            'SELECT COUNT(DISTINCT LOWER(customer)) FROM orders',
        ],
    },
    {
        'id': 'mean_amount',
        'question': 'Average amount among orders whose amount is not null.',
        'guard': (3, 'C03', 'paid', None, '2026-10-12'),
        'holdout': (4, 'C04', 'paid', 0, '2026-10-13'),
        'oracle': lambda rows: sum(r[3] for r in rows if r[3] is not None) / sum(r[3] is not None for r in rows),
        'valid': [
            'SELECT AVG(amount) FROM orders',
            'SELECT 1.0 * SUM(amount) / COUNT(amount) FROM orders',
        ],
        'invalid': [
            'SELECT AVG(COALESCE(amount, 0)) FROM orders',
            'SELECT AVG(amount) FROM orders WHERE amount > 0',
        ],
    },
    {
        'id': 'october_orders',
        'question': 'Number of orders in October 2026, including Oct 1 and excluding Nov 1.',
        'guard': (3, 'C03', 'paid', 30, '2026-11-01'),
        'holdout': (4, 'C04', 'paid', 30, '2026-10-01'),
        'oracle': lambda rows: sum('2026-10-01' <= r[4] < '2026-11-01' for r in rows),
        'valid': [
            "SELECT COUNT(*) FROM orders WHERE day >= '2026-10-01' AND day < '2026-11-01'",
            "SELECT COUNT(*) FROM orders WHERE day BETWEEN '2026-10-01' AND '2026-10-31'",
        ],
        'invalid': [
            "SELECT COUNT(*) FROM orders WHERE day >= '2026-10-01' AND day <= '2026-11-01'",
            "SELECT COUNT(*) FROM orders WHERE day >= '2026-10-02' AND day < '2026-11-01'",
        ],
    },
]


def run_query(rows, query):
    with sqlite3.connect(':memory:') as con:
        con.execute('CREATE TABLE orders (id INTEGER, customer TEXT, status TEXT, amount INTEGER, day TEXT)')
        con.executemany('INSERT INTO orders VALUES (?, ?, ?, ?, ?)', rows)
        return con.execute(query).fetchone()[0]


def same(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    return math.isclose(actual, expected, rel_tol=0, abs_tol=1e-10)


def main():
    parser = argparse.ArgumentParser(description='Reproduce the hand-designed SQL illustration from the paper.')
    parser.add_argument('--output', type=Path, default=Path('artifacts/paper-pilot.json'))
    destination = parser.parse_args().output
    reports = []
    for task in TASKS:
        datasets = {'base': BASE, 'guard': BASE + [task['guard']],
                    'holdout': BASE + [task['holdout']]}
        expected = {name: task['oracle'](rows) for name, rows in datasets.items()}
        for label in ('valid', 'invalid'):
            for index, query in enumerate(task[label], 1):
                actual = {name: run_query(rows, query) for name, rows in datasets.items()}
                matches = {name: same(actual[name], expected[name]) for name in datasets}
                reports.append({'task': task['id'], 'label': label,
                                'candidate': f'{label}_{index}', 'query': query,
                                'oracle': expected, 'actual': actual, 'matches': matches,
                                'accepted_base': matches['base'],
                                'accepted_base_and_guard': matches['base'] and matches['guard']})

    assert len(reports) == 20
    assert all(r['matches']['base'] for r in reports)
    assert all(all(r['matches'].values()) for r in reports if r['label'] == 'valid')
    assert all(not (r['matches']['guard'] and r['matches']['holdout']) for r in reports if r['label'] == 'invalid')

    stats = {}
    for method, accept in [('base_only', 'accepted_base'), ('base_and_guard', 'accepted_base_and_guard')]:
        counts = collections.Counter()
        for r in reports:
            group = r['label']
            decision = 'accepted' if r[accept] else 'rejected'
            counts[f'{group}_{decision}'] += 1
            if r[accept] and not r['matches']['holdout']:
                counts[f'{group}_accepted_but_holdout_failed'] += 1
        stats[method] = dict(sorted(counts.items()))

    output = {'scope': 'Hand-designed synthetic SQLite illustration, not AI training or a representative benchmark.',
              'base_rows_per_task': len(BASE), 'tasks': len(TASKS),
              'valid_candidates': 10, 'invalid_candidates': 10,
              'datasets_per_task': 3, 'statistics': stats, 'candidates': reports}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in output.items() if k != 'candidates'}, indent=2, ensure_ascii=False))
    print(f'Results written: {destination}')


if __name__ == '__main__':
    main()
