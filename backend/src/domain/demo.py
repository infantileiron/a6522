from datetime import date, timedelta
from random import Random


def demo_rows() -> list[dict]:
    rng = Random(26111803)
    rows = []

    def append_assignment(cohort, arm, assigned, delivery_day=None, pickup_day=None, complaint_day=None):
        number = len(rows) + 1
        row = {
            "assignment_id": f"LW-1118-{number:04d}",
            "recipient_id": f"REC-{number:05d}",
            "cohort": cohort,
            "arm": arm,
            "assigned_on": assigned.isoformat(),
        }
        for key, offset in (
            ("delivered_on", delivery_day),
            ("picked_up_on", pickup_day),
            ("complained_on", complaint_day),
        ):
            row[key] = None if offset is None else (assigned + timedelta(days=offset)).isoformat()
        rows.append(row)

    # Counts are fixed per cohort. Seeded shuffling gives varied event histories.
    for cohort, pickup_counts, complaint_counts in (
        ("住宅柜", (130, 162), (5, 3)),
        ("园区柜", (147, 168), (4, 4)),
        ("交通站柜", (158, 153), (3, 4)),
    ):
        for arm_index, arm in enumerate(("A", "B")):
            order = list(range(200))
            rng.shuffle(order)
            pickups = set(order[:pickup_counts[arm_index]])
            complaints = set(rng.sample(range(200), complaint_counts[arm_index]))
            for index in range(200):
                assigned = date(2026, 11, 9 + index % 7)
                delivery = None if index not in complaints and (index + arm_index) % 13 == 0 else 0
                pickup = rng.randrange(3) if index in pickups else (3 + index % 2 if index % 3 == 0 else None)
                complaint = 1 + index % 2 if index in complaints else None
                append_assignment(cohort, arm, assigned, delivery, pickup, complaint)

    for arm in ("A", "B"):
        for index in range(20):
            cohort = ("住宅柜", "园区柜", "交通站柜")[index % 3]
            append_assignment(cohort, arm, date(2026, 11, 16 + index % 2), 0, index % 3)
        for day in (18, 18, 19, 19):
            append_assignment("交通站柜", arm, date(2026, 11, day), 0)
    return rows
