```dafny
// src: source.py:1 | def truncate_number(number: float) -> float:
method truncate_number(number: real) returns (return_value: real)
{
  // src: source.py:10 | return number - int(number)
  return_value := number - PyInt(number) as real;
}
```
