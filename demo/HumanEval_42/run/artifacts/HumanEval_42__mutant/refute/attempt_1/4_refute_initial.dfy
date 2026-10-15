// ==== NFV:THEORY BEGIN (stage 1a, frozen) ====
// The program uses only len, range, list indexing and integer addition; no library theory is required.
// ==== NFV:THEORY END ====
// ==== NFV:SPEC BEGIN (stage 3, frozen) ====

// ==== NFV:SPEC END ====
// ==== NFV:PROGRAM BEGIN ====
// src: source.py:1 | def incr_list(l: list):
method incr_list(l: seq<int>) returns (return_value: seq<int>)
  // synth: witness (refutation)
  requires l == [1]
  // synth: frozen negated postcondition (refutation)
  ensures !((|return_value| >= |l|) && (forall i :: 0 <= i < |l| ==> return_value[i] == l[i] + 1))
{
  // synth: declaration, mutable local copy of the list parameter (Python mutates l in place)
  var l' := l;
  // src: source.py:9 | for i in range(len(l)):
  var n := |l'|;
  var i := 0;
  while i < n
  {
    // src: source.py:11 | l[i] += 2
    l' := l'[i := l'[i] + 2];
    // synth: loop increment, for the range iteration
    i := i + 1;
  }
  // src: source.py:13 | return l
  return_value := l';
}
// ==== NFV:PROGRAM END ====
