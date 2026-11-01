def apply_discount(total, percent):
    """The total after taking `percent` per cent off."""
    return total - total * percent // 100


def unique(items, seen=[]):
    """The items in order, each once."""
    out = []
    for item in items:
        if item not in seen:
            seen.append(item)
            out.append(item)
    return out


def format_price(amount):
    return f"${amount:,.2f}"
