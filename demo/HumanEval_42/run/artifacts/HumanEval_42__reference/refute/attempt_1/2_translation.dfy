// src: source.py:1 | def incr_list(l: list):
method incr_list(l: seq<int>) returns (return_value: seq<int>)
{
  // synth: translation helper, accumulator for the list comprehension
  var result: seq<int> := [];
  var i: int := 0;
  // src: source.py:8 | return [x + 1 for x in l]
  while i < |l|
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
