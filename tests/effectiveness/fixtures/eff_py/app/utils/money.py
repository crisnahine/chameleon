def format_money(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}{currency} {dollars}.{remainder:02d}"
