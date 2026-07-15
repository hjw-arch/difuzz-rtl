FIRRTL version 3.3.0
circuit Top :
  module TargetLeaf :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>
    reg leaf_r : UInt<1>, clock with : (reset => (reset, UInt<1>(0h0)))
    leaf_r <= a
    y <= mux(leaf_r, a, UInt<1>(0h0))

  module TargetRoot :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>
    reg root_r : UInt<1>, clock with : (reset => (reset, UInt<1>(0h0)))
    inst leaf of TargetLeaf
    root_r <= a
    leaf.clock <= clock
    leaf.reset <= reset
    leaf.a <= a
    y <= mux(root_r, leaf.y, UInt<1>(0h0))

  module Outside :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>
    reg outside_r : UInt<2>, clock with : (reset => (reset, UInt<2>(0h0)))
    outside_r <= cat(a, a)
    y <= mux(neq(outside_r, UInt<2>(0h0)), a, UInt<1>(0h0))

  module Top :
    input clock : Clock
    input reset : UInt<1>
    input a : UInt<1>
    output y : UInt<1>
    reg top_r : UInt<1>, clock with : (reset => (reset, UInt<1>(0h0)))
    inst target of TargetRoot
    inst outside of Outside
    top_r <= a
    target.clock <= clock
    target.reset <= reset
    target.a <= a
    outside.clock <= clock
    outside.reset <= reset
    outside.a <= a
    y <= mux(top_r, target.y, outside.y)
