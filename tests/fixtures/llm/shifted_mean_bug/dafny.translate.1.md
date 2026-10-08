```dafny
// src: source.py:1 | def shifted_mean(l):
method shifted_mean(l: seq<real>) returns (return_value: real)
{
  // src: source.py:3 | return (sum(l) + 1) / len(l)  # bug
  return_value := (Sum(l) + 1.0) / (|l| as real);
}
```
