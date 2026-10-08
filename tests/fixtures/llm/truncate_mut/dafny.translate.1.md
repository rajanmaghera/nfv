```dafny
// src: source.py:1 | def truncate_number(number: float) -> float:
method truncate_number(number: real) returns (return_value: real)
{
  // src: source.py:10 | integer_part = int(number)
  var integer_part := PyInt(number);
  // src: source.py:11 | decimal_part = number - integer_part
  var decimal_part := number - integer_part as real;
  // src: source.py:13 | return decimal_part + 1
  return_value := decimal_part + 1.0;
}
```
