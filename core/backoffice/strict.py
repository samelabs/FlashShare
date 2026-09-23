"""Back-office strict scalar gates.

JSON type confusion must NEVER become a plausible operator command:
int()/bool()/str() coercions are banned at the operator boundary. A true
Python int is required for integers (bool is a subclass — rejected);
bool is required for flags; str for text evidence. Anything else is a
typed reject BEFORE the domain command runs.
"""


def strict_int(value, *, allow_zero=True):
    """True int only: bool/float/str/None/container → ValueError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_integer")
    if value == 0 and not allow_zero:
        raise ValueError("zero_amount")
    return value


def strict_bool(value):
    if not isinstance(value, bool):
        raise ValueError("invalid_boolean")
    return value


def strict_str(value, *, max_len=None):
    if not isinstance(value, str):
        raise ValueError("invalid_string")
    v = value.strip()
    if not v:
        raise ValueError("invalid_string")
    if max_len is not None and len(v) > max_len:
        raise ValueError("invalid_string")
    return v
