module {
  firrtl.circuit "Top" {
    firrtl.module private @TargetRoot(in %a: !firrtl.uint<1>, out %y: !firrtl.uint<1>) {
      firrtl.strictconnect %y, %a : !firrtl.uint<1>
    }
    firrtl.module @Detached(in %a: !firrtl.uint<1>, out %y: !firrtl.uint<1>) {
      %target_a, %target_y = firrtl.instance target @TargetRoot(in a: !firrtl.uint<1>, out y: !firrtl.uint<1>)
      firrtl.strictconnect %target_a, %a : !firrtl.uint<1>
      firrtl.strictconnect %y, %target_y : !firrtl.uint<1>
    }
    firrtl.module @Top(in %a: !firrtl.uint<1>, out %y: !firrtl.uint<1>) attributes {convention = #firrtl<convention scalarized>} {
      %target_a, %target_y = firrtl.instance target @TargetRoot(in a: !firrtl.uint<1>, out y: !firrtl.uint<1>)
      firrtl.strictconnect %target_a, %a : !firrtl.uint<1>
      firrtl.strictconnect %y, %target_y : !firrtl.uint<1>
    }
  }
}
