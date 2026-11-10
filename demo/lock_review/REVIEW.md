# Review of commit `pre`

1. **BLOCKER** `parse_qty` accepts negative quantities.

```python
import pytest
from inventory import parse_qty


def test_negative_refused():
    with pytest.raises(ValueError):
        parse_qty("-3")
```

2. **MAJOR** `parse_qty("0")` may not return 0.

```python
from inventory import parse_qty


def test_zero():
    assert parse_qty("0") == 0
```
