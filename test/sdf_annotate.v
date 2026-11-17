`timescale 1ns / 1ps

// Loads the routed delays (SDF) into the gate-level netlist. Used by sdf.mk;
// SDF_FILE is set on the compiler command line.
module sdf_annotate ();
  initial $sdf_annotate(`SDF_FILE, tb.user_project);
endmodule
