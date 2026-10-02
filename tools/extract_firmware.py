#!/usr/bin/env python3
"""
Reproduce firmware acquisition for the Elgato Key Light line.

Elgato's Control Center Windows installer bundles per-model firmware images. This script:
  1. (manual) download the installer, e.g.
       https://edge.elgato.com/egc/windows/eccw/1.8.2/ControlCenter_1.8.2.714_x64.msi
  2. parse the .msi (an OLE / CFBF compound file) and reassemble the embedded CAB stream by
     walking the FAT sector chain -- a raw byte-carve of the CAB fails because the stream is
     fragmented across non-contiguous OLE sectors.
  3. run `cabextract` on the reassembled CAB to get Firmware_<model>.bin.

Usage:
    python3 extract_firmware.py ControlCenter_1.8.2.714_x64.msi outdir/
Requires: cabextract on PATH.
"""
import sys, struct, os, subprocess

def reassemble_cab(msi_path, cab_out):
    d = open(msi_path, "rb").read()
    assert d[:8] == bytes.fromhex("d0cf11e0a1b11ae1"), "not an OLE compound file"
    u32 = lambda o: struct.unpack("<I", d[o:o+4])[0]
    ssz = 1 << struct.unpack("<H", d[30:32])[0]           # sector size (usually 4096)
    dirStart = u32(48)
    difatStart, nDifat = u32(68), u32(72)
    ENDOFCHAIN, FREESECT = 0xFFFFFFFE, 0xFFFFFFFF
    sect = lambda k: d[(k+1)*ssz:(k+1)*ssz+ssz]
    # DIFAT -> FAT sector list
    difat = list(struct.unpack("<109I", d[76:76+109*4]))
    s = difatStart
    while s not in (ENDOFCHAIN, FREESECT) and nDifat > 0:
        ents = struct.unpack(f"<{ssz//4}I", sect(s)); difat += list(ents[:-1]); s = ents[-1]
    FAT = []
    for fs in (x for x in difat if x not in (FREESECT, ENDOFCHAIN)):
        FAT += list(struct.unpack(f"<{ssz//4}I", sect(fs)))
    def chain(start):
        out = bytearray(); s = start
        while s not in (ENDOFCHAIN, FREESECT) and s < len(FAT):
            out += sect(s); s = FAT[s]
        return bytes(out)
    # directory: find the large stream whose reassembled head is 'MSCF'
    dirbytes = chain(dirStart)
    for i in range(0, len(dirbytes), 128):
        e = dirbytes[i:i+128]
        if len(e) < 128:
            break
        nlen = struct.unpack("<H", e[64:66])[0]
        if nlen == 0 or e[66] != 2:            # type 2 == stream
            continue
        size = struct.unpack("<Q", e[120:128])[0]
        if size < 1_000_000:
            continue
        data = chain(struct.unpack("<I", e[116:120])[0])[:size]
        if data[:4] == b"MSCF":
            open(cab_out, "wb").write(data)
            return cab_out, size
    raise SystemExit("no MSCF (CAB) stream found in MSI")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    msi, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    cab = os.path.join(outdir, "payload.cab")
    _, sz = reassemble_cab(msi, cab)
    print(f"reassembled CAB: {sz} bytes -> {cab}")
    subprocess.run(["cabextract", "-d", outdir, cab], check=True)
    print("done; Firmware_*.bin extracted into", outdir)
