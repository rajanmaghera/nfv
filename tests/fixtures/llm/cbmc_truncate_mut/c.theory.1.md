`int()` on a float truncates towards zero, which is exactly C's conversion to an integer type, so the only
library code needed is the shared bound:

```c
#define NFV_MAX_LEN 5
#include <stdbool.h>
// doc: int(x): "For floating-point numbers, this truncates towards zero." -> C cast (long long)x
```
