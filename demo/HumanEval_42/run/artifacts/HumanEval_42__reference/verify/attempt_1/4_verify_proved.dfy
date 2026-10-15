// ==== NFV:THEORY BEGIN (stage 1a, frozen) ====
// No library calls beyond the core language (list comprehension, integer addition).
// ==== NFV:THEORY END ====
// ==== NFV:SPEC BEGIN (stage 3, frozen) ====

// ==== NFV:SPEC END ====
// ==== NFV:PROGRAM BEGIN ====
// src: source.py:1 | def incr_list(l: list):
method incr_list(l: seq<int>) returns (return_value: seq<int>)
  // synth: frozen postcondition (stage 3)
  ensures |return_value| >= |l|
  // synth: frozen postcondition (stage 3)
  ensures forall i :: 0 <= i < |l| ==> return_value[i] == l[i] + 1
{
  // synth: translation helper, accumulator for the list comprehension
  var result: seq<int> := [];
  var i: int := 0;
  // src: source.py:8 | return [x + 1 for x in l]
  while i < |l|
    // synth: invariant
    invariant 0 <= i <= |l|
    // synth: invariant
    invariant |result| == i
    // synth: invariant
    invariant forall j :: 0 <= j < i ==> result[j] == l[j] + 1
    // synth: decreases
    decreases |l| - i
  {
    // src: source.py:8 | return [x + 1 for x in l]
    var x := l[i];
    result := result + [x + 1];
    // synth: translation helper, loop index increment
    i := i + 1;
  }
  // src: source.py:8 | return [x + 1 for x in l]
  return_value := result;
  return;
}
// ==== NFV:PROGRAM END ====
