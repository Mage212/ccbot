"""Benchmark HTML converter vs entities converter on bracket-heavy inputs.

Run:
    uv run python scripts/benchmark_entities_converter.py
"""

from __future__ import annotations

from multiprocessing import Process, Queue
import statistics
import time

from ccbot.entities_converter import render_markdown_to_entities
from ccbot.html_converter import convert_markdown

SIZES = [10_000, 50_000, 100_000]
RUNS = 3
TIMEOUT_SECONDS = 15.0


def _worker(queue: Queue[float], label: str, text: str) -> None:
    started = time.perf_counter()
    if label == "html_converter":
        convert_markdown(text)
    else:
        render_markdown_to_entities(text)
    queue.put(time.perf_counter() - started)


def _run_once_with_timeout(label: str, text: str, timeout: float) -> float | None:
    queue: Queue[float] = Queue(maxsize=1)
    process = Process(target=_worker, args=(queue, label, text))
    process.start()
    process.join(timeout=timeout)
    if process.is_alive():
        process.kill()
        process.join()
        return None
    if queue.empty():
        return None
    return queue.get()


def bench(label: str, text: str) -> float | None:
    measurements: list[float] = []
    for _ in range(RUNS):
        elapsed = _run_once_with_timeout(label, text, TIMEOUT_SECONDS)
        if elapsed is None:
            print(f"  {label:16s} timeout>{TIMEOUT_SECONDS:.1f}s")
            return None
        measurements.append(elapsed)
    avg = statistics.mean(measurements)
    print(
        f"  {label:16s} avg={avg:8.4f}s "
        f"min={min(measurements):8.4f}s max={max(measurements):8.4f}s"
    )
    return avg


def main() -> None:
    print("Benchmark: bracket-heavy markdown conversion")
    print(f"runs per case: {RUNS}")
    print(f"timeout per run: {TIMEOUT_SECONDS:.1f}s")

    for n in SIZES:
        text = "[" * (n // 2) + "x" + "]" * (n // 2)
        print(f"\ninput length={len(text)}")
        html_avg = bench("html_converter", text)
        ent_avg = bench("entities_converter", text)
        if html_avg is not None and ent_avg is not None and ent_avg > 0:
            print(f"  speedup: {html_avg / ent_avg:8.2f}x")


if __name__ == "__main__":
    main()
