#!/usr/bin/env python3
"""
Apply the SO_RCVTIMEO fix to Elgato Key Light (Gen 1) firmware (Realtek RTL8195A).

Root cause: SimpleHTTPD_Socket_Accept @0x3000c508 sets SO_KEEPALIVE + TCP_KEEPIDLE/INTVL/CNT
on each accepted connection but never sets a receive timeout, so a slow/trickling client holds
the single HTTP connection slot indefinitely (device needs a power cycle).

Fix: repurpose the 4th setsockopt call (TCP_KEEPCNT, optname 5) in place into
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &10000ms, 4)
The lwIP in this image (lwip_setsockopt @0x100228f5, SO_RCVTIMEO handler @0x10022a4c) accepts
optlen==4 as an int-milliseconds value. Patch is 26 bytes + 2 NOPs = 30 bytes, exactly the size
of the original call-4 block (0x3000c5dc..0x3000c5fa), so no code cave / relocation is needed.

Assemble the stub (produces the 26 patch bytes below):
    arm-none-eabi-as -mthumb -mcpu=cortex-m3 patch.s -o patch.o
    arm-none-eabi-ld -Ttext=0x3000c5dc --defsym setsockopt_veneer=0x3000b8ac patch.o -o patch.elf
    arm-none-eabi-objcopy -O binary patch.elf patch.bin

patch.s:
    movs r0,#4  ; str r0,[sp,#0]        ; optlen=4
    movw r0,#10000 ; str r0,[sp,#4]     ; timeout ms
    add  r3,sp,#4                       ; &timeout
    movw r2,#0x1006                     ; SO_RCVTIMEO
    movw r1,#0x0fff                     ; SOL_SOCKET
    mov  r0,r5                          ; fd (held in r5 across the function)
    bl   0x3000b8ac                     ; setsockopt veneer -> lwip_setsockopt @0x100228f5

NOTE: this produces a code-correct image. Getting a patched image installed on a real device
still requires going through the vendor's own firmware update mechanism, which is out of scope
for this tool, see the README.
"""
import sys

BASE = 0x30000000
FS   = 142296                              # file offset that maps to 0x30000000 (section 2)
SITE = FS + (0x3000c5dc - BASE)            # = 192948, call-4 block
ORIG = bytes.fromhex('0420009001ab052200f0d2f838b140f2ed60009023460ff2601200f01bfc')  # 30 B
# assembled stub (26 B) + NOP NOP
PATCH = bytes.fromhex('0420009042f21070019001ab41f2060240f6ff712846fff75bf9') + bytes.fromhex('00bf00bf')

def main(inp, outp):
    d = bytearray(open(inp, 'rb').read())
    got = bytes(d[SITE:SITE+30])
    if got != ORIG:
        sys.exit(f"refusing: bytes at {SITE:#x} don't match the expected call-4 block\n"
                 f"  got={got.hex()}\n  exp={ORIG.hex()}")
    assert len(PATCH) == 30
    d[SITE:SITE+30] = PATCH
    open(outp, 'wb').write(bytes(d))
    print(f"patched {inp} -> {outp} (SO_RCVTIMEO=10000ms at {SITE:#x})")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: patch_rcvtimeo.py Firmware_Key_Light.bin Firmware_Key_Light_patched.bin")
    main(sys.argv[1], sys.argv[2])
