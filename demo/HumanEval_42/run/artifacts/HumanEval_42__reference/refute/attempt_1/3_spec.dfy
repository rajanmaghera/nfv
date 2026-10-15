
ensures |return_value| >= |l|
ensures forall i :: 0 <= i < |l| ==> return_value[i] == l[i] + 1
