Adding 1 once to the total instead of once per element is wrong whenever len(l) != 1. With l = [0, 0] the
function returns (0 + 1) / 2 = 1/2 while the postcondition demands 0/2 + 1 = 1.

```dafny
requires l == [0.0, 0.0]
```
