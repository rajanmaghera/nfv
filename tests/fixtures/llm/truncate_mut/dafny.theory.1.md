The only library call is `int(number)` on a float, documented as "For floating-point numbers, this truncates
towards zero."

```dafny
// doc: int(x): "For floating-point numbers, this truncates towards zero."
function PyInt(x: real): int
{ if x >= 0.0 then x.Floor else -((-x).Floor) }
```
