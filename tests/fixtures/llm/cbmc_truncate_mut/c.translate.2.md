The conversion `(long long)number` overflows for huge or infinite doubles. Python's int() has no such limit, so
the harness bounds the input to the range where the C model is faithful:

```c
// synth: declarations of the later stages and the nondet source
bool nfv_pre(double number);
bool nfv_spec(double number, double return_value);
double nondet_double(void);

// src: source.py:1 | def truncate_number(number: float) -> float:
double truncate_number(double number)
{
  // src: source.py:10 | integer_part = int(number)
  long long integer_part = (long long)number;
  // src: source.py:11 | decimal_part = number - integer_part
  double decimal_part = number - (double)integer_part;
  // src: source.py:13 | return decimal_part + 1
  return decimal_part + 1;
}

// synth: harness
void nfv_harness(void)
{
  double number = nondet_double();
  // synth: bound, keep the float-to-int conversion in range
  __CPROVER_assume(number > -1e15 && number < 1e15);
  __CPROVER_assume(nfv_pre(number));
  double return_value = truncate_number(number);
  __CPROVER_assert(nfv_spec(number, return_value), "nfv_spec");
}
```
