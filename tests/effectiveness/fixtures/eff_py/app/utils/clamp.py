def clamp(value: int, low: int, high: int) -> int:
    if low > high:
        raise ValueError("low must be <= high")
    return min(max(value, low), high)
