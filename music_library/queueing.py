"""Small, deterministic queue builders used by the local library."""

from __future__ import annotations

from collections import defaultdict, deque
from heapq import heapify, heappop, heappush
from random import Random
from typing import Iterable, Mapping


def unique_tracks(tracks: Iterable[Mapping[str, object]], *, include_repeats: bool) -> list[dict[str, object]]:
    """Keep first occurrence order unless the caller explicitly wants repeats."""
    result: list[dict[str, object]] = []
    seen: set[object] = set()
    for track in tracks:
        track_id = track["id"]
        if not include_repeats and track_id in seen:
            continue
        seen.add(track_id)
        result.append(dict(track))
    return result


def true_shuffle(tracks: Iterable[Mapping[str, object]], *, seed: int | None = None) -> list[dict[str, object]]:
    result = [dict(track) for track in tracks]
    Random(seed).shuffle(result)
    return result


def variety_shuffle(
    tracks: Iterable[Mapping[str, object]],
    *,
    cooldown: int = 4,
    seed: int | None = None,
) -> list[dict[str, object]]:
    """Interleave creators while keeping work close to O(n log creators).

    The cooldown is softened only when the selected pool makes separation
    impossible. Tracks with no creator share the explicit ``Unknown creator``
    bucket instead of being treated as all different.
    """
    rng = Random(seed)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for track in tracks:
        creator = str(track.get("creator") or "Unknown creator").strip().casefold() or "Unknown creator"
        grouped[creator].append(dict(track))

    for bucket in grouped.values():
        rng.shuffle(bucket)

    heap: list[tuple[int, float, str]] = [(-len(bucket), rng.random(), creator) for creator, bucket in grouped.items()]
    heapify(heap)
    blocked: deque[tuple[int, int, float, str]] = deque()
    result: list[dict[str, object]] = []

    while heap or blocked:
        while blocked and blocked[0][0] <= len(result):
            _, remaining, tie_breaker, creator = blocked.popleft()
            heappush(heap, (remaining, tie_breaker, creator))

        if not heap:
            # Every remaining creator is still cooling down. Relax the oldest
            # restriction rather than stalling the queue.
            _, remaining, tie_breaker, creator = blocked.popleft()
            heappush(heap, (remaining, tie_breaker, creator))

        remaining, _, creator = heappop(heap)
        result.append(grouped[creator].pop())
        remaining += 1  # Negative remaining count after taking one item.
        if remaining < 0:
            blocked.append((len(result) + max(0, cooldown), remaining, rng.random(), creator))

    return result
