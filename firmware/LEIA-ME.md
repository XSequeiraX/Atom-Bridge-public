# Firmware do S3: onde está e como gravar

Uma só imagem estável serve os **três usos**: **GoPro**, **F6 com o Atom** e **F6 autónomo**. O modo escolhe-se depois de gravar
(não há um ficheiro por modo). Versão estável actual: **gopro_gw 21** (em `estavel/`).

```
firmware/
  gravar.py                              grava no S3 (esptool); ver abaixo
  estavel/gopro_gw-21_only_program.bin   só a aplicação (actualização, mantém o Wi-Fi e as GoPro do S3)
  estavel/gopro_gw-21_complete.bin       imagem completa (S3 novo ou avariado; apaga tudo)
  m5stick/f6_remote-6_complete.bin       comando de bolso do F6 (M5StickS3), imagem completa
  m5stick/f6_remote-6_only_program.bin   comando de bolso do F6, só a aplicação (actualizar)
```

O firmware de teste (a sonda BLE) já não está aqui: vive em `../testes/` (ver `testes/LEIA-ME.md`).
`python gravar.py COM4 --f6-probe` grava-o, só a partir do repositório.

## Gravar (qualquer sistema com python)

```
pip install esptool
python gravar.py COM4             # estável, só a aplicação: o Wi-Fi e as GoPro do S3 ficam
python gravar.py COM4 --completo  # estável, imagem completa: apaga tudo (S3 novo, avariado ou vindo do simulador)
```

- Troca `COM4` pela porta do S3 (Linux: `/dev/ttyACM0`). `--dry-run` mostra os comandos sem os correr.
- O S3 tem de estar ligado **ao PC**, com um cabo de dados. Se estava em modo F6 (USB host) não aparece porta série: liga-o com o
  botão **BOOT carregado**.
- Vindo da sonda (`f6_ble_probe`) ou de um S3 em branco, usa `--completo`. Depois de `--completo` o S3 perdeu o Wi-Fi, o
  token e as GoPro: refaz a configuração pelo Atom (ver abaixo).
- O mesmo `.bin` serve a M5Stack StampS3 e a Seeed XIAO ESP32S3.

## Comando de bolso do F6 (M5StickS3)

REC/STOP do F6 numa M5Stack M5StickS3, só com o S3 do F6 em **F6 standalone**. O nome e a palavra-passe do AP vão no momento do flash:

```
python gravar.py COM7 --m5stick --ssid F6-REMOTE-1A2B     # pede a palavra-passe no terminal; imagem completa + Wi-Fi
python gravar.py COM7 --m5stick                           # só a aplicação (o Wi-Fi guardado fica)
python gravar.py COM7 --m5stick --so-wifi --ssid OUTRO    # só muda o AP
```

**Pela consola do Atom** (sem PC): ligar a M5StickS3 por USB ao Atom e, em **ESC > DEVICES > M5STICK (F6 REMOTE)**: *Flash a M5StickS3* (nova, com o Wi-Fi),
*Update its program* (o Wi-Fi fica) ou *Change its Wi-Fi only*. Pede o nome do AP (`F6-REMOTE-xxxx`) e a palavra-passe, que só vão para a M5StickS3.
Se não responder: premir o botão de reset lateral cerca de 2 s até o LED verde piscar e tentar outra vez.

Botões, ecrã (3 páginas: A trancado vira a página), bloqueio (B três vezes, A a segurar) e o que ainda não foi testado na placa: `_esp32_tools/m5stick_f6_remote/LEIA-ME.txt`.

## Depois de gravar: escolher o modo

**GoPro** (modo de origem)
1. Liga o S3 ao Atom por USB e na consola: DEVICES > GATEWAYS (USB) > *Setup / update by USB* (grava o Wi-Fi e o token).
2. Acrescenta as GoPro: linha do S3 > *Add GoPro*.

**F6 com o Atom**
1. Consola: GATEWAYS > S3 > *Mode* > **F6 with the Atom**. O S3 reinicia; as GoPro ficam guardadas.
2. Liga o cabo do F6 ao S3 **primeiro**, depois no F6: MENU > SYSTEM > USB > FRC-8 > connect.

**F6 autónomo** (sem Atom)
1. O S3 já tem de ter sido configurado pelo Atom por USB (precisa do token).
2. Consola: GATEWAYS > S3 > *Mode* > **F6 standalone** e escolhe uma palavra-passe de 8 a 63 caracteres
   (ou, no portal `GOPRO-S3-XXXX` em `http://192.168.4.1`: System > Mode > F6 standalone).
3. Cabo do F6 ao S3 primeiro, FRC-8 > connect no F6. No telemóvel: Wi-Fi `F6-REMOTE-xxxx`, abre `http://192.168.4.1/`.

Voltar atrás: menu *System* da página (`...`) ou a consola do Atom.

## De onde vêm os ficheiros

A fonte é `_esp32_tools/gopro_gw/gopro_gw.ino`. O GitHub compila-a e guarda o resultado em `atom/app/fw/s3/` (é o que o Atom grava
nos S3). **Esta pasta é uma cópia** dessa versão para levares a um PC sem o repositório; se o firmware subir de versão, copia os dois
ficheiros de `atom/app/fw/s3/` para `estavel/` (`gopro_gw.bin` vira `..._only_program.bin` e `gopro_gw.merged.bin` vira `..._complete.bin`) e muda o número no nome (e em `gravar.py`).

O firmware da M5StickS3 tem duas cópias iguais: `firmware/m5stick/` (para o PC) e `atom/app/fw/m5stick/` (o que o Atom grava pela consola, sem o número da versão no nome:
`f6_remote_complete.bin` e `f6_remote_only_program.bin`). Ao compilar uma versão nova do `_esp32_tools/m5stick_f6_remote`, actualizar as duas.
