"""
data_structures.py
===================
SmartPark KE — core data structures.

This module deliberately implements (rather than hides behind a library) the
data structures the assignment (Task 1, part b) asks us to justify. Each
class below is used by services/*.py and documents WHY it was chosen.

Data structures used, and why:

1. MinHeap (heapq wrapper)          -> free-bay allocation
   Free bays are kept in a binary min-heap keyed on bay number. Allocating
   the lowest-numbered free bay (pop) and returning a released bay (push)
   are both O(log n), which matters because entry/exit events can arrive
   in quick bursts at the barrier. A plain sorted list would cost O(n) to
   re-sort on every release.

2. Hash Map (Python dict)           -> active-session lookup
   At exit, the attendant/camera reads a number plate and we must find the
   matching open session instantly. A dict keyed on plate number gives
   expected O(1) lookup instead of scanning every open session (O(n)).

3. Deque (collections.deque)        -> overflow waiting queue
   When the lot is full, arriving vehicles queue at the gate. A FIFO queue
   is the fair, correct model (first to arrive, first let in) and deque
   gives O(1) append/pop from both ends (needed if a driver in the queue
   abandons and leaves).

4. Singly Linked List (custom)      -> per-vehicle visit history / audit
   chain
   Each completed session is appended to an immutable, append-only chain
   per plate number (and a global audit chain). We only ever read this
   sequentially (print a statement, replay for VAT reconciliation) and
   never need random access or re-sorting, so a linked list is a natural,
   memory-light fit and makes the "no record is ever edited, only
   appended" audit guarantee easy to reason about.

5. Sorted tiers + binary search (bisect) -> fee computation
   Parking rate tiers (0-30 min free, up to 2h, up to 4h, up to 6h, 6h+)
   are stored sorted by their upper-bound minute. Looking up the fee for a
   given duration is a binary search, O(log n) in the number of tiers, and
   because tiers are just data (not code), management can add/remove/edit
   tiers at runtime without a redeploy -- this satisfies the "change
   rates without a software change" requirement.
"""

import heapq
from collections import deque
from bisect import bisect_right


# ---------------------------------------------------------------------------
# 1. MinHeap of free bay numbers
# ---------------------------------------------------------------------------
class FreeBayHeap:
    """Min-heap of available bay numbers.

    allocate() -> pops and returns the lowest-numbered free bay: O(log n)
    release(n) -> pushes a bay back onto the heap when a vehicle exits:
                  O(log n)
    peek_count() -> O(1) count of currently free bays, for the live board.
    """

    def __init__(self, bay_numbers):
        self._heap = list(bay_numbers)
        heapq.heapify(self._heap)          # O(n) one-off build

    def allocate(self):
        if not self._heap:
            return None                    # lot is full
        return heapq.heappop(self._heap)   # O(log n)

    def release(self, bay_number):
        heapq.heappush(self._heap, bay_number)  # O(log n)

    def peek_count(self):
        return len(self._heap)

    def free_set(self):
        return set(self._heap)


# ---------------------------------------------------------------------------
# 2. Hash map of active sessions (plate -> session_id) lives in the caller
#    as a plain dict; no wrapper class is needed, Python's dict already
#    gives O(1) average lookup/insert/delete. See services/slot_manager.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. FIFO overflow queue
# ---------------------------------------------------------------------------
class OverflowQueue:
    """FIFO queue of plate numbers waiting for a bay when the lot is full."""

    def __init__(self):
        self._q = deque()

    def enqueue(self, plate_number):
        self._q.append(plate_number)             # O(1)

    def dequeue(self):
        return self._q.popleft() if self._q else None  # O(1)

    def remove(self, plate_number):
        """A waiting driver gives up and leaves -- O(n) worst case but the
        queue is expected to stay short (bounded by physical kerb space)."""
        try:
            self._q.remove(plate_number)
            return True
        except ValueError:
            return False

    def __len__(self):
        return len(self._q)

    def as_list(self):
        return list(self._q)


# ---------------------------------------------------------------------------
# 4. Singly linked list for append-only audit / visit history
# ---------------------------------------------------------------------------
class _Node:
    __slots__ = ("value", "next")

    def __init__(self, value):
        self.value = value
        self.next = None


class AuditChain:
    """An append-only singly linked list.

    We keep a tail pointer so append() is O(1). Traversal for reporting is
    O(n), which is fine because reports are generated on demand, not on
    every request. Records are never mutated or removed, which mirrors the
    real-world requirement of an auditable, tamper-evident financial trail.
    """

    def __init__(self):
        self._head = None
        self._tail = None
        self._count = 0

    def append(self, value):
        node = _Node(value)
        if self._tail is None:
            self._head = self._tail = node
        else:
            self._tail.next = node
            self._tail = node
        self._count += 1

    def __len__(self):
        return self._count

    def __iter__(self):
        node = self._head
        while node is not None:
            yield node.value
            node = node.next

    def to_list(self):
        return list(self)


# ---------------------------------------------------------------------------
# 5. Sorted rate tiers + binary search
# ---------------------------------------------------------------------------
class RateTable:
    """Holds parking-fee tiers sorted by their upper-bound minute and finds
    the applicable fee via binary search (bisect).

    Each tier is a tuple: (upper_bound_minutes, amount_kshs, label).
    upper_bound_minutes == None means "no upper bound" (open-ended top tier)
    and is stored internally as float('inf') so bisect can compare it.

    Because tiers are ordinary data rows (see models.RateTier), an admin can
    insert/edit/delete a tier through the admin UI and RateTable.reload()
    picks it up immediately -- no code change, no redeploy.
    """

    def __init__(self, tiers):
        self.reload(tiers)

    def reload(self, tiers):
        # tiers: iterable of (upper_bound_minutes_or_None, amount, label)
        normalised = [
            (float("inf") if t[0] is None else t[0], t[1], t[2])
            for t in tiers
        ]
        normalised.sort(key=lambda t: t[0])
        self._bounds = [t[0] for t in normalised]
        self._tiers = normalised

    def fee_for(self, duration_minutes):
        """Binary search for the first tier whose upper bound is >= the
        given duration. O(log n) in the number of tiers."""
        if not self._bounds:
            return 0, "No rate configured"
        idx = bisect_right(self._bounds, duration_minutes)
        if idx == 0:
            idx = 0
        if idx >= len(self._tiers):
            idx = len(self._tiers) - 1
        # bisect_right gives the insertion point; the correct tier is the
        # first one whose bound is not exceeded by duration_minutes.
        for bound, amount, label in self._tiers:
            if duration_minutes <= bound:
                return amount, label
        # duration exceeds every bound -> fall back to the last (open) tier
        bound, amount, label = self._tiers[-1]
        return amount, label

    def as_list(self):
        return [
            (None if b == float("inf") else b, a, l)
            for b, a, l in self._tiers
        ]
