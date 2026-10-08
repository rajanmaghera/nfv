(Pilot-run style: the model saturates outside the long long range instead of restricting the domain.)

```c
#define NFV_MAX_LEN 5
#include <stdbool.h>
#include <limits.h>
// doc: int(x) truncates towards zero; out-of-range values saturate (UNFAITHFUL: Python has big ints)
long long py_int(double x)
{
  if (x != x) return 0;
  if (x >= 9223372036854775808.0) return LLONG_MAX;
  if (x < -9223372036854775808.0) return LLONG_MIN;
  return (long long)x;
}
```
