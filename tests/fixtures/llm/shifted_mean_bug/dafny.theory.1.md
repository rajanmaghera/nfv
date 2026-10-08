The program only calls the built-in `sum` (and `len`, which maps to `|l|`). Its documentation says it returns
the start value (0) plus the items, and the start value on an empty iterable, which is a simple recursion:

```dafny
// doc: sum(iterable, start=0) returns start plus the items of the iterable;
// doc: when the iterable is empty, return the start value.
function Sum(l: seq<real>): real
{ if |l| == 0 then 0.0 else l[0] + Sum(l[1..]) }
```
