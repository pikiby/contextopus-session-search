"""Compact bars and timestamped histories. Missing measurements remain gaps."""
import math


def bar(value, width=16):
    if value is None or not math.isfinite(value):
        return 'N/A'
    count = round(max(0, min(100, value)) * width / 100)
    return '[' + '█' * count + '░' * (width-count) + ']'


def history(samples, now, seconds=120, width=28, unit='%'):
    """Fixed time window, labelled magnitude scale fitted to the visible values."""
    width = max(16, width)
    columns = [None] * width
    for stamp, value in samples:
        if now-seconds <= stamp <= now:
            index = min(width-1, int((stamp-(now-seconds))/seconds * width))
            columns[index] = max(0, value) if value is not None and math.isfinite(value) else None
    peak = max((v for v in columns if v is not None), default=0)
    magnitude = 10 ** math.floor(math.log10(max(1, peak)))
    top = next(step*magnitude for step in (1, 2, 5, 10) if step*magnitude >= max(1, peak))
    lines = []
    for row in range(3, 0, -1):
        threshold = top * row / 3
        def cell(value):
            if value is None:
                return ' '
            fraction = max(0, min(1, (value-(threshold-top/3))/(top/3)))
            return ' ▁▂▃▄▅▆▇█'[math.ceil(fraction*8)]
        label = f'{top:g}{unit}' if row == 3 else ''
        lines.append(f'{label:>3} ' + ('┤' if row == 3 else '│') + ''.join(cell(v) for v in columns))
    # Zero measurements are on the baseline; unknown columns remain gaps.
    lines.append(f'{"0"+unit:>3} └' + ''.join('•' if v == 0 else '─' for v in columns))
    axis = list(' ' * width)
    for start, label in ((0, f'−{seconds}s'), (width//2-2, f'−{seconds//2}s'), (width-3, 'now')):
        axis[start:start+len(label)] = label
    lines.append('     ' + ''.join(axis))
    return '\n'.join(lines)


def estimated_time(remaining, rate, observed_seconds, *, active=True):
    """ETA for discovered fragments, using a complete rolling minute only."""
    if remaining == 0:
        return '0m'
    if remaining is None or not active or not rate or (observed_seconds or 0) < 60:
        return 'Calculating…' if active else 'N/A'
    minutes = math.ceil(remaining / rate)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return '~' + (f'{days}d ' if days else '') + (f'{hours}h ' if hours or days else '') + f'{minutes}m'
