# Transport scheduling

## Overview

Deterministic solver — no AI in this layer. After class groups are assigned to periods with students allocated, the solver calculates which students need to travel between campuses for each period and assigns buses accordingly.

Periods are `layout_periods` rows, which already carry `day_number` and `period_number`.
Movements that fall outside any period (before school, after school) are written with
`layout_period_id` NULL and `day_number` set.

---

## Demand calculation

For each period in each day of the cycle:

```python
async def calculate_demand(attempt_id: str) -> dict:
    """
    Returns: {(layout_period_id, day, from_campus, to_campus): student_count}
    """
    # Get all students assigned to classes on Campus B in each period
    # whose home campus is Campus A — they need transport A→B
    entries = await db.fetch("""
        SELECT
            te.layout_period_id,
            te.campus_id as class_campus_id,
            s.campus_id as home_campus_id,
            te.day_number,
            COUNT(tes.student_id) as student_count
        FROM timetable_entries te
        JOIN timetable_entry_students tes ON tes.timetable_entry_id = te.id
        JOIN students s ON s.id = tes.student_id
        WHERE te.attempt_id = $1
        AND te.campus_id != s.campus_id  -- only cross-campus
        GROUP BY te.layout_period_id, te.campus_id, s.campus_id, te.day_number
    """, attempt_id)

    demand = {}
    for entry in entries:
        key = (entry['layout_period_id'], entry['day_number'],
               entry['home_campus_id'], entry['class_campus_id'])
        demand[key] = entry['student_count']

    return demand
```

---

## Bus assignment

```python
async def assign_buses(demand: dict, buses: list, routes: list) -> list:
    assignments = []
    bus_state = {bus['id']: None for bus in buses}  # current campus of each bus

    # Sort demand by departure time so we can chain bus movements
    for (period_id, day, from_campus, to_campus), students_needed in sorted(demand.items()):
        if students_needed == 0:
            continue

        route = find_route(routes, from_campus, to_campus)
        trips_needed = ceil(students_needed / max_bus_capacity(buses))

        for trip in range(trips_needed):
            # Find a bus currently at from_campus, or nearest
            bus = find_available_bus(buses, bus_state, from_campus, period_id, day)

            # If bus is not at from_campus, it needs an empty leg first
            if bus_state[bus['id']] != from_campus and bus_state[bus['id']] is not None:
                assignments.append({
                    'bus_id': bus['id'],
                    'from_campus_id': bus_state[bus['id']],
                    'to_campus_id': from_campus,
                    'is_empty_leg': True,
                    'day_number': day,
                    'layout_period_id': period_id,
                    'passenger_count': 0,
                })

            # Now the passenger trip
            capacity = min(bus['capacity'], students_needed)
            students_needed -= capacity
            departure_time, arrival_time = calculate_times(period_id, route)

            assignments.append({
                'bus_id': bus['id'],
                'from_campus_id': from_campus,
                'to_campus_id': to_campus,
                'is_empty_leg': False,
                'day_number': day,
                'layout_period_id': period_id,
                'passenger_count': capacity,
                'departure_time': departure_time,
                'arrival_time': arrival_time,
            })

            # Update bus state
            bus_state[bus['id']] = to_campus

    return assignments
```

---

## Day chain

Each bus has a chain of movements across the full day. The solver builds this explicitly so there are no implied empty legs — every repositioning movement is a record.

```
Bus 1 — Day 1:
  08:45  A → B  (38 passengers)
  09:10  B → A  (empty leg — repositioning for next run)
  11:05  A → B  (24 passengers)
  11:25  B → A  (empty leg)
  15:00  A → B  (after school — 31 passengers)
  15:22  B home (end of day, returns home campus)
```

---

## Bus duty link

After transport schedule is written, Layer 8 (bus duty assignment) uses departure times to set exact duty slot times for bus supervisors. The duty_slot start_time is set to 15 minutes before departure, end_time to departure + buffer.
