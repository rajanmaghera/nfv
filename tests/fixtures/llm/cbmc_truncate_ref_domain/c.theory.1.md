C's `long long` cannot hold Python's int of a huge float, so the model is restricted to the range where it is
faithful:

```c
#define NFV_MAX_LEN 5
#include <stdbool.h>
// doc: int(x): "For floating-point numbers, this truncates towards zero."
long long py_int(double x)
{
  NFV_MODEL_DOMAIN(x > -9.2e18 && x < 9.2e18);
  return (long long)x;
}
```
