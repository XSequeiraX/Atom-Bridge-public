# Testes: tudo o que é experiência, fora do caminho do produto

Nada aqui é usado pelo Atom, pela consola nem pelo firmware estável (`firmware/`). Fica guardado para retomar quando houver dados novos.

| Pasta | O que é | Estado |
|---|---|---|
| `f6_ble_probe/` | Sonda BLE por porta série (scan, connect, escrita e leitura, varrimentos). | Experiência parada. |
| `f6_usb_ltc/` | `f6_frc.py` (cliente FRC-8 por USB-MIDI, origem do protocolo usado em `atom/app/frc8.py`), `ltc_gen.py` + `analyze_f6_take.py` (gerar LTC e medir o sincronismo de uma take do F6). | Ferramentas de ensaio. |

## O que se sabe sobre o iPhone (F6 Control)

- A app é um central BLE directo (CoreBluetooth), sem MIDI. O binário está cifrado: os UUIDs não se leem.
- Ideias futuras (central BLE para um BTA-1 verdadeiro, protocolo da Tilta, API local): issues #15, #16 e #29.
- O protocolo BLE das apps de controlo da Zoom (F8 e F8n) está descrito num projecto público, sob Creative Commons Attribution 4.0:
  https://github.com/Ashtry197/zoom-f8-ble-protocol. Este repositório não leva material tirado do código das apps da Zoom.

## Gravar um teste no S3

`python firmware/gravar.py COM4 --f6-probe`. Apaga o S3 inteiro: voltar ao gateway estável com `--completo`
e refazer a configuração pelo Atom (ver `firmware/LEIA-ME.md`).

## Compilar

Pasta com o mesmo nome do `.ino` (já é assim), FQBN do `.github/workflows/s3-firmware.yml`
(`esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PartitionScheme=default_8MB,FlashMode=dio`), core esp32 3.1.1 e NimBLE-Arduino 2.5.1.
