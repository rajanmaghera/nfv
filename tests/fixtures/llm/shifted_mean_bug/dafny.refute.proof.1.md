The verifier needs `Sum` unfolded on the two-element witness:

```dafny
// ==== NFV:THEORY BEGIN (stage 1a, frozen) ====
// doc: sum(iterable, start=0) returns start plus the items of the iterable;
// doc: when the iterable is empty, return the start value.
function Sum(l: seq<real>): real
{ if |l| == 0 then 0.0 else l[0] + Sum(l[1..]) }
// ==== NFV:THEORY END ====
// ==== NFV:SPEC BEGIN (stage 3, frozen) ====

// ==== NFV:SPEC END ====
// ==== NFV:PROGRAM BEGIN ====
// src: source.py:1 | def shifted_mean(l):
method shifted_mean(l: seq<real>) returns (return_value: real)
  // synth: frozen precondition (stage 1b)
  requires |l| > 0
  // synth: witness (refutation)
  requires l == [0.0, 0.0]
  // synth: frozen negated postcondition (refutation)
  ensures !((return_value == Sum(l) / (|l| as real) + 1.0))
{
  // src: source.py:3 | return (sum(l) + 1) / len(l)  # bug
  return_value := (Sum(l) + 1.0) / (|l| as real);
  // synth: proof, unfold Sum on the witness
  assert Sum(l) == l[0] + Sum(l[1..]);
  assert Sum(l[1..]) == l[1] + Sum(l[1..][1..]);
  assert l[1..][1..] == [];
}
// ==== NFV:PROGRAM END ====
```
