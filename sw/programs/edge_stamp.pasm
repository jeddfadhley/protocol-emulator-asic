.program edge_stamp
; Logic-analyser style edge timestamping. For every rising and falling edge
; on in_base + 0, pushes the 16-bit TIME at which it was seen (alternating
; rising, falling). With CLKDIV = 0 the resolution is one clock.
; Config: autopush, push_thresh 16.
.wrap_target
    wait 1 edge 0
    in time, 16
    wait 0 edge 0
    in time, 16
.wrap
