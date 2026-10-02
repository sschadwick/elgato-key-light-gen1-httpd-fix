.syntax unified
.thumb
.text
.global _start
_start:
    movs r0, #4            @ optlen = 4
    str  r0, [sp, #0]
    movw r0, #10000        @ SO_RCVTIMEO = 10000 ms
    str  r0, [sp, #4]
    add  r3, sp, #4        @ optval ptr
    movw r2, #0x1006       @ SO_RCVTIMEO
    movw r1, #0x0fff       @ SOL_SOCKET
    mov  r0, r5            @ fd
    bl   setsockopt_veneer @ 0x3000b8ac
