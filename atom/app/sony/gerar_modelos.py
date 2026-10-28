"""Le o CrDefines.h do SDK da Sony instalado e escreve modelos.inc: {"ILCE-7M4", CrCameraDeviceModel_ILCE_7M4}, ...
Assim um SDK mais novo traz os modelos novos sem mexer no sony_ctl.cpp. Uso: gerar_modelos.py <CrDefines.h> <saida>"""
import re
import sys

texto = open(sys.argv[1], encoding="utf-8", errors="replace").read()
bloco = re.search(r"enum\s+CrCameraDeviceModelList[^{]*\{(.*?)\}", texto, re.S)
if not bloco:
    sys.exit("CrCameraDeviceModelList not found in %s" % sys.argv[1])
nomes = re.findall(r"CrCameraDeviceModel_(\w+)", bloco.group(1))
with open(sys.argv[2], "w") as f:
    for n in nomes:
        # ILCE_7M4 -> ILCE-7M4, DSC_RX0M2 -> DSC-RX0M2: o nome do modelo como a Sony o escreve
        f.write('    {"%s", CrCameraDeviceModel_%s},\n' % (n.replace("_", "-"), n))
print("%d models" % len(nomes))
