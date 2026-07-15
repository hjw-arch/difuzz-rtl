module tb;
  logic clock = 0;
  logic reset = 0;
  logic a = 0;
  logic metaReset = 0;
  wire y;

`ifdef INSTRUMENTED
  wire [29:0] io_covSum;
  wire metaAssert;
  Top dut (.*);
`else
  Top dut (.clock, .reset, .a, .y);
`endif

  task automatic tick(input logic nextReset, nextA, nextMetaReset);
    reset = nextReset;
    a = nextA;
    metaReset = nextMetaReset;
    #1 clock = 1;
    #1 $display("TRACE %0b", y);
    clock = 0;
    #1;
  endtask

  initial begin
    tick(1, 0, 0);
    tick(0, 1, 0);
    tick(0, 1, 1);
    tick(0, 0, 1);
    tick(0, 1, 0);
    tick(0, 1, 1);
    $finish;
  end
endmodule
