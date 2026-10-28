# Licenças e avisos

## O código do Atom Bridge

O código deste repositório (bridge, consola, página, instalador, firmware dos gateways, ferramentas) está sob a
**licença MIT**: ver [`LICENSE`](LICENSE). Os ficheiros de terceiros abaixo **não** estão sob a MIT: cada um mantém a
sua licença.

O Atom Bridge é um projeto independente. **Não é feito, aprovado nem suportado pela Sony, pela Canon, pela Blackmagic
Design, pela GoPro, pela Zoom nem pela Deity.** Os nomes das câmaras e dos aparelhos servem só para dizer com o que é
compatível; as marcas são dos respetivos donos. O suporte do Atom Bridge é dado só pelos seus autores.

## Ficheiros de terceiros no repositório

| Ficheiros | O que são | Licença |
|---|---|---|
| `atom/app/sony/libCr_Core.so`, `atom/app/sony/CrAdapter/` | Bibliotecas do Sony Camera Remote SDK v2.02.00 | Sony Camera Remote SDK License Agreement (ver abaixo) |
| dentro dessas bibliotecas: libssh2, OpenSSL, libusb | Componentes open source do SDK da Sony | BSD-3-Clause, Apache-2.0, LGPL-2.1+: [`atom/app/sony/OSS-NOTICES.md`](atom/app/sony/OSS-NOTICES.md) |
| `atom/app/fonts/DMSans-*.woff2` | Tipo de letra DM Sans, Copyright 2014 The DM Sans Project Authors | SIL Open Font License 1.1: [`atom/app/fonts/OFL.txt`](atom/app/fonts/OFL.txt) |
| `atom/app/fonts/JetBrainsMono-*.woff2` | Tipo de letra JetBrains Mono, Copyright 2020 The JetBrains Mono Project Authors | SIL Open Font License 1.1: [`atom/app/fonts/OFL.txt`](atom/app/fonts/OFL.txt) |
| `atom/app/gopro_qr.html` (a biblioteca `qrcode-generator` 1.4.4, embebida) | Gera o QR code da hora para a GoPro | MIT, Copyright (c) 2009 Kazuhiko Arase (texto abaixo) |
| `*.bin` do S3 e do M5Stick (`atom/app/fw/`, `firmware/`, `testes/`) | Firmware compilado a partir do código deste repositório | O nosso código é MIT; leva compilados o Arduino core para ESP32 (LGPL-2.1+, com partes do ESP-IDF em Apache-2.0), o NimBLE-Arduino (Apache-2.0) e, no M5Stick, o M5Unified e o M5GFX (MIT) |
| MicroPython do C3 (descarregado na instalação, não está no repositório) | Firmware base do gateway C3 | MIT |

O firmware dos ESP32 compila-se a partir do código aberto deste repositório e das versões fixas das bibliotecas
(`.github/workflows/s3-firmware.yml`, `_esp32_tools/`). Quem quiser pode recompilá-lo com outra versão das
bibliotecas LGPL. As fontes dessas bibliotecas estão em https://github.com/espressif/arduino-esp32,
https://github.com/h2zero/NimBLE-Arduino, https://github.com/m5stack/M5Unified e https://github.com/m5stack/M5GFX.

### qrcode-generator (MIT)

Copyright (c) 2009 Kazuhiko Arase, https://github.com/kazuhikoarase/qrcode-generator

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice
shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE
AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

## Sony (Camera Remote SDK)

Só vem no repositório o nosso `sony_ctl` já compilado, com as bibliotecas da Sony de que precisa ao lado. A licença da
Sony deixa distribuí-las **só como parte da aplicação**, sem as separar nem usar sozinhas. O SDK em si (headers,
documentação, exemplos) **não** vem: para recompilar o `sony_ctl` é preciso descarregá-lo da Sony e aceitar a licença
dela. O texto completo do aviso está em [`atom/app/sony/AVISO-SONY.md`](atom/app/sony/AVISO-SONY.md).

**Antes de usar o Atom Bridge** (com qualquer câmara), o bridge mostra um aviso de licenças geral, igual para todos os
aparelhos, atuais e futuros, e pede o consentimento do operador: uma só vez, antes de a consola carregar e na primeira
visita da página (`atom/app/licencas.py`). Sem aceitar, nada funciona. Para as bibliotecas da Sony, o aviso inclui:

1. Uma câmara Sony usada ou controlada através desta aplicação pode ficar fora da garantia do fabricante, como a Sony
   especifica para essa câmara (na Austrália, Nova Zelândia, Filipinas, África do Sul e Iraque valem as garantias da
   lei do consumidor).
2. O Atom Bridge não é da Sony nem é suportado por ela.
3. É proibido usar o software, a aplicação ou as câmaras para armas, para transportar explosivos, armas químicas ou
   biológicas ou outros artigos perigosos, ou em atividades militares de informações, vigilância ou reconhecimento.
4. O software é fornecido "tal como está", sem garantia, e não serve para usos de alto risco.

## Canon (CCAPI)

As Canon são controladas pelo **Camera Control API (CCAPI)**, a API HTTP que corre na própria câmara. O repositório
**não traz nenhum ficheiro, biblioteca ou documentação da Canon**: o `atom/app/canon_ccapi.py` é código nosso que fala
com essa API. A documentação do CCAPI é confidencial e nunca entra no repositório. Para ativar o CCAPI numa câmara é
preciso a ferramenta da própria Canon, que cada um obtém no programa de developers da Canon.

O acordo de SDK da Canon (Canon Europa N.V.) aplica-se na Europa, EEE, Reino Unido, Médio Oriente e África. Quem
distribuir o Atom Bridge com o suporte às Canon tem de dar a conhecer aos utilizadores finais os termos mínimos da
Canon:

> End User Licence Agreement [minimum terms]. You hereby acknowledge and agree that:
> 1. the [Software Solutions] include Canon's proprietary and confidential information ("Modifiable Files") which are
>    licensed in accordance with Canon's then current terms;
> 2. you shall only use the Modifiable Files with the [Software Solutions];
> 3. Save to the extent and in the circumstances expressly permitted by applicable law, you shall not disassemble,
>    reverse assemble, reverse compile or otherwise reverse engineer or modify any portion of the Modifiable Files;
> 4. You shall ensure that the Modifiable Files are not distributed in a way that affect Canon's or its licensors
>    ownership including without limitation under the terms of a copyleft open source licence.

Por isso o Atom Bridge não usa licenças copyleft no seu código. Não se usam logótipos da Canon.

## Blackmagic Design

O repositório **não traz nada do SDK da Blackmagic** (nem bibliotecas, nem código de exemplo, nem documentação): o
acordo da Blackmagic não deixa distribuir o SDK e só permite usar o código de exemplo internamente. A BMPCC é
controlada pelo protocolo Bluetooth da própria câmara, e os telemóveis pela API REST da app Blackmagic Camera. A única
forma permitida de referir os produtos é "Atom Bridge compatible with Blackmagic Design <produto>"; não se usam marcas
nem nomes que se confundam com os da Blackmagic.

## GoPro, Zoom, Deity

Não há SDKs nem ficheiros destes fabricantes no repositório. As GoPro são controladas pela API Bluetooth aberta da
GoPro (Open GoPro), o Zoom F6 pelo protocolo USB-MIDI do comando FRC-8 e o TC-1 pela sua interface de timecode.
