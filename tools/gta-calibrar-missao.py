#!/usr/bin/env python3
"""Descobre qual variavel do script marca "estou em missao" no GTA SA.

O endereco de $ONMISSION e citado por ai, mas citar nao e verificar: este
script tira uma foto das variaveis globais fora da missao e outra durante ela,
e mostra quais mudaram de 0 para 1. O que sobrar dessa peneira e o candidato.

    tools/gta-calibrar-missao.py fora      # com o CJ na rua, sem missao
    tools/gta-calibrar-missao.py dentro    # depois de comecar uma missao
"""
import os
import pathlib
import struct
import sys

SCRIPT_SPACE = 0xA49960
TAMANHO = 0x4000          # 16 KB de variaveis globais ja cobrem as usadas
FOTO = pathlib.Path("/tmp/gta-globals-{}.bin")


def gta_pid():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if b"gta-sa.exe" not in pathlib.Path(f"/proc/{entry}/cmdline").read_bytes():
                continue
            if "00400000-" in pathlib.Path(f"/proc/{entry}/maps").read_text(errors="replace"):
                return int(entry)
        except OSError:
            continue
    return None


def ler_globais(pid):
    with open(f"/proc/{pid}/mem", "rb", 0) as mem:
        mem.seek(SCRIPT_SPACE)
        return mem.read(TAMANHO)


def main():
    momento = sys.argv[1] if len(sys.argv) > 1 else ""
    if momento not in ("fora", "dentro"):
        sys.exit(__doc__)

    pid = gta_pid()
    if not pid:
        sys.exit("o GTA San Andreas nao esta aberto")

    dados = ler_globais(pid)
    FOTO.with_name(FOTO.name.format(momento)).write_bytes(dados)
    print(f"foto '{momento}' guardada ({len(dados)} bytes)")

    fora = FOTO.with_name(FOTO.name.format("fora"))
    dentro = FOTO.with_name(FOTO.name.format("dentro"))
    if not (fora.exists() and dentro.exists()):
        print("agora rode o outro momento para comparar")
        return

    a, b = fora.read_bytes(), dentro.read_bytes()
    candidatos = []
    for off in range(0, min(len(a), len(b)) - 4, 4):
        antes = struct.unpack("<i", a[off:off + 4])[0]
        depois = struct.unpack("<i", b[off:off + 4])[0]
        if antes == 0 and depois == 1:
            candidatos.append(off)

    print(f"\nvariaveis que foram de 0 para 1: {len(candidatos)}")
    for off in candidatos[:20]:
        print(f"  $({off // 4})  offset 0x{off:X}  endereco 0x{SCRIPT_SPACE + off:X}")
    if len(candidatos) > 20:
        print(f"  ... e mais {len(candidatos) - 20}")


if __name__ == "__main__":
    main()
