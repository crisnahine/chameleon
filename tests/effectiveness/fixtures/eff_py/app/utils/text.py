def truncate_text(text: str, max_length: int) -> str:
    if max_length <= 1:
        return text[:max_length]
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"
